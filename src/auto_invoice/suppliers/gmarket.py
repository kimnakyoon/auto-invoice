"""지마켓(Gmarket) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://my.gmarket.co.kr/ko/pc/detail/basic/<장바구니번호>
  (화면에 보이는 "주문번호"와는 다른, URL 전용 내부 번호다. 파싱할 필요 없이
  URL 그대로 열면 된다.)
- "배송조회" 버튼을 누르면 뜨는 모달은 tracking.gmarket.co.kr 도메인의
  iframe이라서 메인 페이지 DOM에서는 텍스트를 못 읽는다 - 반드시 그 프레임
  안에서 읽어야 한다. 모달 안에는 "택배사명 송장번호"가 같은 줄에 붙어서
  나온다 (예: "CJ택배 501707425705").
- 지마켓은 롯데온(Imperva)과 별개로 Cloudflare Turnstile 봇 확인 화면이 뜬다.
  번들 크로미엄에서는 사람이 직접 체크박스를 눌러도 "확인 중..."에서 넘어가지
  않는 경우가 있었다 (자동화 브라우저 자체를 의심하는 것으로 보임). 그래서
  조회를 우리가 직접 실행한 진짜 크롬(CDP, WANTS_CDP_CHROME)에서 한다 -
  이 크롬에서는 봇 확인이 아예 안 뜨고, 떠도 몇 초 만에 저절로 풀린다(옥션
  실측). 그래도 감지되면 풀리기를 기다렸다가 진행하고, 안 풀리면
  BlockedError로 알린다.
- 로그인이 안 되어 있으면 mobile.gmarket.co.kr/Login/Login?URL=<원래주소>로
  리다이렉트된다. 로그인 폼 셀렉터는 옥션(같은 이베이코리아 통합 로그인)과
  거의 같다: 아이디 input#typeMemberInputId, 비밀번호
  input#typeMemberInputPassword. 로그인 버튼만 옥션(#btnLogin)과 달리
  button#btn_memberLogin이다. 사용자가 "첫 로그인부터 자동"을 요청했고,
  실측해보니 쿠키 없는 새 브라우저로 로그인 페이지를 열어도 봇 확인 화면이
  뜨지 않았고 캡차 입력칸(#typeMemberCaptcha)도 DOM에는 있지만 화면에는
  보이지 않았다. 그래서 GMARKET_ID/GMARKET_PW 환경변수로 완전 자동 로그인한다
  (SSG/더현대/NS홈쇼핑/11번가/옥션과 동일한 패턴).
- 로그인 버튼을 누르면 mobile.gmarket.co.kr/login/loginProc(빈 중간 페이지)
  을 거쳐 원래 주소로 돌아온다(2026-09-02 실측: 0.3초 -> 0.8초). 이 중간
  페이지에는 비밀번호 칸이 없어 _looks_like_login_page가 "로그인 화면 아님"
  이라 하는데, 그 순간 주문상세로 goto하면 **아직 도는 중인 리다이렉트를
  끊어서 쿠키가 다 붙기 전에 다시 로그인 페이지로 튕긴다** - 2026-09-02 16:31
  실행에서 그렇게 "로그인 후에도 여전히 로그인 페이지" 실패가 나고, 한 번
  막히면 나머지 27건이 전부 실패로 남았다(평소에는 1.5초 안에 체인이 끝나
  안 걸렸다). 그래서 로그인 뒤에는 주소가 /login 바깥으로 나올 때까지
  기다리고(_wait_for_login_redirects), 그래도 로그인 페이지면 잠깐 뒤 한 번
  더 가본 다음에야 포기한다.
- 로그인 실패는 화면 문구가 아니라 **alert()** 으로 알려준다 (실측: 없는
  아이디로 시도하면 "아이디 확인 후 다시 입력해 주세요."). 롯데온과 같은
  방식이라, dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다. 핸들러가
  없으면 Playwright가 alert을 조용히 닫아버려서 원인도 모른 채 대기 시간만
  다 쓰고 실패한다.
- 비밀번호를 저장하고 싶지 않은 경우를 위해, GMARKET_PW가 비어 있으면
  예전처럼 아이디만 자동 입력하고 사람이 직접 로그인하는 경로로 넘어간다
  (롯데온과 동일).
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#typeMemberInputId"
LOGIN_PW_SELECTOR = "#typeMemberInputPassword"
LOGIN_BUTTON_SELECTOR = "#btn_memberLogin"
# 반복 실패 등으로 캡차가 요구되면 이 입력칸이 화면에 보인다 (평소에는 DOM에만
# 있고 숨겨져 있다). 보이면 자동 로그인은 포기하고 사람에게 넘긴다.
LOGIN_CAPTCHA_SELECTOR = "#typeMemberCaptcha"

DOMAINS = {"gmarket.co.kr", "www.gmarket.co.kr", "my.gmarket.co.kr"}
SITE_KEY = "gmarket"

DEFAULT_COURIER = "택배"  # 모달에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초
# 로그인 직후 리다이렉트 체인(Login -> login/loginProc -> 원래 주소)이 끝나기를
# 기다리는 최대 시간. 실측 0.8초, 느린 날을 감안해 넉넉히.
LOGIN_REDIRECT_SETTLE_MS = 15 * 1000
BOT_CHECK_WAIT_TIMEOUT_MS = 3 * 60 * 1000  # 봇 확인 통과 대기 최대 3분

TRACKING_BUTTON_TEXTS = ["배송조회"]

# 모달(iframe) 안, 주소/배송요청사항 뒤에 "택배사명 송장번호"가 붙어서 나온다.
TRACKING_LINE_PATTERN = re.compile(r"([가-힣A-Za-z]{2,20})\s*([0-9][0-9\-]{7,})\s*$")
# 같은 규칙을 여러 줄짜리 텍스트 전체에 걸 때 쓴다(모달이 다 그려졌는지 볼 때).
# 위 규칙은 줄 끝($)에 걸려 있어서, 줄 단위로 쪼개지 않고 그대로 search하면
# 맨 마지막 줄만 보게 된다.
TRACKING_LINE_ANY_PATTERN = re.compile(TRACKING_LINE_PATTERN.pattern, re.MULTILINE)
NOT_YET_PATTERNS = ["배송준비중", "상품준비중", "결제확인중", "주문확인중"]
BOT_CHECK_PATTERNS = ["사람인지 확인", "봇(Bot)이란", "로봇이 아닙니다"]

# 조회를 우리가 직접 실행한 진짜 크롬(CDP)에서 한다는 표시 (orchestrator.py).
# 번들 크로미엄에서는 요청이 빠르면 "로봇이 아닙니다" 봇 확인이 떠서 한동안
# 요청 간격을 6~12초로 늘려 피했는데(2026-09-01), 옥션에서 확인한 대로 진짜
# 크롬(CDP)은 봇 확인이 아예 안 뜨고 떠도 저절로 풀리므로, 간격을 늘리는 대신
# 브라우저를 바꾸고 간격은 기본값(1.5~4초)으로 되돌렸다. 실행 중 크롬 창이
# 하나 뜬다 (옥션과 별도 프로필 auth/chrome_profile_gmarket).
WANTS_CDP_CHROME = True


def extract_order_id(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        raise ParseError(f"URL에서 주문 식별 번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _is_login_flow_url(url: str) -> bool:
    """로그인 폼(Login)과 그 처리 중간 페이지(login/loginProc) 둘 다 해당한다."""
    return "signinssl.gmarket.co.kr" in url or "/login" in url.lower()


def _looks_like_login_page(page) -> bool:
    return common.looks_like_login_page(page, _is_login_flow_url)


def _wait_for_login_redirects(page) -> None:
    """로그인 직후 리다이렉트 체인이 원래 주소까지 다 돌기를 기다린다.

    주소가 /login 바깥으로 나오면 체인이 끝난 것이다. 그 뒤 화면이 그려질
    때까지 한 번 더 기다려, 뒤따르는 goto가 체인을 끊지 않게 한다(맨 위
    docstring). 시간 안에 안 나와도 예외는 내지 않는다 - 호출자가 goto 뒤에
    로그인 페이지인지 다시 본다.
    """
    common.wait_for_url(page, lambda url: not _is_login_flow_url(url), LOGIN_REDIRECT_SETTLE_MS)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LOGIN_REDIRECT_SETTLE_MS)
    except Exception:  # noqa: BLE001 - 다음 goto가 어차피 다시 확인한다
        pass
    page.wait_for_timeout(500)


def _looks_like_bot_check(page) -> bool:
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    return any(p in body_text for p in BOT_CHECK_PATTERNS)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("GMARKET_ID"))


def _captcha_is_visible(page) -> bool:
    """캡차 입력칸이 실제로 화면에 보이는지. 평소에는 DOM에만 있고 숨겨져 있어서,
    존재 여부(count)가 아니라 보이는지로 판단해야 한다."""
    locator = page.locator(LOGIN_CAPTCHA_SELECTOR)
    if locator.count() == 0:
        return False
    try:
        return locator.first.is_visible()
    except Exception:
        return False


def _auto_login(page) -> bool:
    """GMARKET_ID/GMARKET_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    옥션(같은 이베이코리아 통합 로그인) 어댑터와 같은 패턴이지만, 지마켓은
    로그인 실패를 화면 문구가 아니라 alert()으로 알려주기 때문에 롯데온처럼
    dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다.

    비밀번호가 설정되어 있지 않거나 캡차가 요구되면 False를 돌려주고, 호출자가
    기존의 수동 로그인 방식으로 넘어간다.
    """
    login_id = os.environ.get("GMARKET_ID")
    login_pw = os.environ.get("GMARKET_PW")
    if not login_id or not login_pw:
        return False
    if _captcha_is_visible(page):
        # 캡차 이미지를 읽어서 푸는 건 우회 시도라 하지 않는다 - 사람에게 넘긴다.
        common.safe_print("[gmarket] 캡차가 요구되어 자동 로그인을 건너뜁니다.")
        return False

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    try:
        page.fill(LOGIN_ID_SELECTOR, login_id)
        page.fill(LOGIN_PW_SELECTOR, login_pw)
        page.click(LOGIN_BUTTON_SELECTOR)

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인
            # 자체는 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를
            # alert보다 먼저 본다 (롯데온과 동일한 순서).
            if not _looks_like_login_page(page):
                # 로그인 폼은 벗어났지만 중간 페이지(loginProc)일 수 있다 - 원래
                # 주소까지 돌아올 때까지 기다린 뒤에 돌려준다(맨 위 docstring).
                _wait_for_login_redirects(page)
                return True
            if alerts:
                raise BlockedError(f"지마켓 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1500

        if _captcha_is_visible(page):
            raise BlockedError(
                "지마켓 로그인에 캡차가 요구됐습니다. --headless 없이 실행해 직접 로그인해주세요."
            )
        raise BlockedError(
            "지마켓 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
            "(추가 본인인증을 요구받았을 수 있습니다 - 브라우저 창을 확인해주세요)."
        )
    finally:
        page.remove_listener("dialog", _on_dialog)


def _wait_for_manual_login(page) -> bool:
    """비밀번호 입력창이 사라질 때까지(=로그인 완료) 화면 상태를 폴링하며 대기한다."""
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _wait_for_bot_check_to_clear(page) -> bool:
    elapsed_ms = 0
    while elapsed_ms < BOT_CHECK_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(3000)
        elapsed_ms += 3000
        if not _looks_like_bot_check(page):
            return True
    return False


def _click_tracking_button(page) -> bool:
    for text in TRACKING_BUTTON_TEXTS:
        loc = page.get_by_text(text, exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _read_tracking_frame_text(page) -> str:
    """배송조회 모달은 tracking.gmarket.co.kr iframe이라 메인 페이지 DOM에는
    텍스트가 없다 - 그 프레임 안에서 읽어야 한다."""
    for frame in page.frames:
        if "tracking.gmarket.co.kr" in frame.url:
            try:
                return frame.inner_text("body")
            except Exception:
                continue
    return ""


def _select_by_order_option(lines: list[str], matched_line_indices: list[int], order_option: str | None):
    """샵마인 엑셀의 "주문옵션" 값이 어느 송장번호 줄 근처(보통 상품명/옵션은
    송장번호 줄보다 앞에 나온다)에만 유일하게 나타나면 그 인덱스를 쓴다.
    0개(표기가 안 맞음) 또는 2개 이상(애매함) 매칭되면 None - 호출자가
    기존 방식(사람 확인 요청)으로 넘어간다."""
    if len(matched_line_indices) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    candidates = []
    prev_idx = -1
    for idx in matched_line_indices:
        # window 시작을 이전 매치 줄 이후로 묶어서, 앞 상품의 옵션 텍스트가
        # 다음 상품 판단에 섞여 들어가지(bleed) 않게 한다.
        window = "\n".join(lines[max(prev_idx + 1, idx - 15) : idx])
        if target in normalize_option(window):
            candidates.append(idx)
        prev_idx = idx
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page, order_id: str, order_option: str | None = None) -> TrackingResult:
    clicked = _click_tracking_button(page)
    if not clicked:
        body_text = page.inner_text("body")
        # 페이지를 연 직후의 검사를 통과한 뒤에 봇 확인으로 바뀌었을 수 있다 -
        # 그대로 두면 '버튼을 못 찾았다'는 엉뚱한 실패 사유가 남는다.
        if any(p in body_text for p in BOT_CHECK_PATTERNS):
            raise BlockedError(
                "지마켓 봇 확인 화면이 떴습니다 (주문상세). 브라우저에서 직접 통과한 뒤 다시 실행해주세요."
            )
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orderId={order_id}).")
        raise_if_cancelled(body_text, order_id)
        raise ParseError(f"배송조회 버튼을 찾지 못했습니다 (orderId={order_id}).")

    # 모달(iframe)이 그려질 때까지만 기다린다 - 예전에는 버튼을 누르고 무조건
    # 1.5초를 잤다. 끝내 송장번호 줄이 안 보이면 예전과 같은 1.5초를 채운다.
    frame_text = common.wait_for_match(
        page, lambda: _read_tracking_frame_text(page), TRACKING_LINE_ANY_PATTERN)
    if not frame_text:
        raise ParseError(f"배송조회 모달(iframe)에서 내용을 읽지 못했습니다 (orderId={order_id}).")

    lines = frame_text.splitlines()
    tracking_matches: dict[int, re.Match] = {}
    for idx, line in enumerate(lines):
        m = TRACKING_LINE_PATTERN.search(line.strip())
        if m:
            tracking_matches[idx] = m

    if not tracking_matches:
        if any(p in frame_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orderId={order_id}).")
        raise_if_cancelled(frame_text, order_id)
        raise ParseError(f"모달에서 송장번호 텍스트를 찾지 못했습니다 (orderId={order_id}).")

    distinct_tracking_nos = {re.sub(r"[^0-9]", "", m.group(2)) for m in tracking_matches.values()}
    matched_idx = _select_by_order_option(lines, list(tracking_matches.keys()), order_option)
    if matched_idx is None:
        if len(distinct_tracking_nos) > 1:
            # 한 주문이 상품별로 나눠 배송되어 서로 다른 송장번호가 여러 개
            # 보이는 경우다 - 어느 걸 써야 하는지 확신할 수 없어 사람이
            # 확인하게 한다 (무신사 어댑터와 동일한 안전 규칙).
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (orderId={order_id}) - 상품별로 나눠 배송된 것으로 보입니다.")
        matched_idx = max(tracking_matches)  # 기존 동작(마지막 매치) 유지

    tracking_match = tracking_matches[matched_idx]
    courier = common.normalize_courier(tracking_match.group(1).strip() or DEFAULT_COURIER)
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


# 조회에 재사용하는 탭 (컨텍스트당 하나). 주문마다 탭을 열고 닫으면 그 비용이
# 매번 드는 데다, 눈에 보이는 크롬 창(CDP)에서는 탭이 주문 수만큼 깜빡인다.
# 어차피 조회는 매번 goto로 시작하므로 이전 주문의 화면이 남아 있어도 상관없다.
_LOOKUP_PAGE: dict[int, object] = {}


def _lookup_page(context: BrowserContext):
    page = _LOOKUP_PAGE.get(id(context))
    if page is None or page.is_closed():
        browser_mod.block_heavy_resources(context)
        page = context.new_page()
        _LOOKUP_PAGE[id(context)] = page
    return page


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_id = extract_order_id(product_url)
    page = _lookup_page(context)
    page.goto(product_url, wait_until="domcontentloaded")

    if _looks_like_bot_check(page):
        # 진짜 크롬(CDP)에서는 Turnstile이 몇 초 만에 저절로 풀린다(옥션과
        # 같은 원리). 창이 항상 떠 있으므로 headless 설정과 무관하게, 안
        # 풀리면 사람이 그 창에서 직접 통과할 수도 있다.
        common.safe_print("[gmarket] 봇 확인 화면이 떴습니다. 저절로 풀리기를 기다립니다 (뜬 크롬 창에서 직접 통과해도 됩니다).")
        if not _wait_for_bot_check_to_clear(page):
            raise BlockedError("봇 확인 대기 시간(3분)이 지났습니다. 통과 후 다시 실행해주세요.")
        page.goto(product_url, wait_until="domcontentloaded")

    if _looks_like_login_page(page):
        if _auto_login(page):
            common.safe_print("[gmarket] 로그인 세션이 없어 자동 로그인했습니다.")
        else:
            # GMARKET_PW가 없거나 캡차가 요구된 경우 - 크롬 창이 항상 떠
            # 있으므로(CDP) 사람이 직접 로그인할 때까지 기다린다.
            _prefill_login_id(page)
            common.safe_print("[gmarket] 아이디는 자동으로 입력했습니다. 뜬 크롬 창에서 비밀번호를 입력하고 로그인해주세요.")
            common.safe_print("[gmarket] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            _wait_for_login_redirects(page)
        common.goto_settled(page, product_url)
        if _looks_like_login_page(page):
            # 로그인 쿠키가 아직 다 안 붙었을 수 있다 - 잠깐 뒤 한 번만 더 가본다.
            page.wait_for_timeout(2000)
            common.goto_settled(page, product_url)
        if _looks_like_login_page(page):
            raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

    # 주문상세는 자바스크립트로 그려진다 - 주문번호가 화면에 뜨면 다 그려진 것이다.
    common.wait_for_text(page, order_id)
    # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
    return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_id, order_option))
