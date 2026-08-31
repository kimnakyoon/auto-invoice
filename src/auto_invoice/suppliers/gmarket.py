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
  이 화면은 사람이 직접 체크박스를 눌러도 "확인 중..."에서 넘어가지 않는
  경우가 있었다 (자동화 브라우저 자체를 의심하는 것으로 보임). 우회를
  시도하지 않고, 롯데온의 Imperva 차단과 동일하게 BlockedError로 알리고
  사람이 직접 해결하도록 한다. 다만 아래 로그인 실측에서는 이 화면이 뜨지
  않았다 - 상품/주문 페이지 쪽에서만 뜬 것으로 보인다.
- 로그인이 안 되어 있으면 mobile.gmarket.co.kr/Login/Login?URL=<원래주소>로
  리다이렉트된다. 로그인 폼 셀렉터는 옥션(같은 이베이코리아 통합 로그인)과
  거의 같다: 아이디 input#typeMemberInputId, 비밀번호
  input#typeMemberInputPassword. 로그인 버튼만 옥션(#btnLogin)과 달리
  button#btn_memberLogin이다. 사용자가 "첫 로그인부터 자동"을 요청했고,
  실측해보니 쿠키 없는 새 브라우저로 로그인 페이지를 열어도 봇 확인 화면이
  뜨지 않았고 캡차 입력칸(#typeMemberCaptcha)도 DOM에는 있지만 화면에는
  보이지 않았다. 그래서 GMARKET_ID/GMARKET_PW 환경변수로 완전 자동 로그인한다
  (SSG/더현대/NS홈쇼핑/11번가/옥션과 동일한 패턴).
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
BOT_CHECK_WAIT_TIMEOUT_MS = 3 * 60 * 1000  # 봇 확인 통과 대기 최대 3분

TRACKING_BUTTON_TEXTS = ["배송조회"]

# 모달(iframe) 안, 주소/배송요청사항 뒤에 "택배사명 송장번호"가 붙어서 나온다.
TRACKING_LINE_PATTERN = re.compile(r"([가-힣A-Za-z]{2,20})\s*([0-9][0-9\-]{7,})\s*$")
NOT_YET_PATTERNS = ["배송준비중", "상품준비중", "결제확인중", "주문확인중"]
BOT_CHECK_PATTERNS = ["사람인지 확인", "봇(Bot)이란"]


def extract_order_id(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        raise ParseError(f"URL에서 주문 식별 번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    if "signinssl.gmarket.co.kr" not in page.url and "/login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


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
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인
            # 자체는 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를
            # alert보다 먼저 본다 (롯데온과 동일한 순서).
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"지마켓 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함

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
            page.wait_for_timeout(1500)  # 모달/iframe 렌더링 대기
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
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orderId={order_id}).")
        raise_if_cancelled(body_text, order_id)
        raise ParseError(f"배송조회 버튼을 찾지 못했습니다 (orderId={order_id}).")

    frame_text = _read_tracking_frame_text(page)
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


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_id = extract_order_id(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_bot_check(page):
            if headless:
                raise BlockedError(
                    "지마켓 봇 확인(Cloudflare) 화면이 떴습니다. 먼저 --headless 없이 실행해 수동으로 통과해주세요."
                )
            common.safe_print("[gmarket] 봇 확인 화면이 떴습니다. 뜬 브라우저 창에서 직접 체크박스를 눌러 통과해주세요.")
            if not _wait_for_bot_check_to_clear(page):
                raise BlockedError("봇 확인 대기 시간(3분)이 지났습니다. 통과 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if _auto_login(page):
                common.safe_print("[gmarket] 로그인 세션이 없어 자동 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "지마켓 로그인이 필요합니다. .env에 GMARKET_PW를 넣으면 자동 로그인하고, "
                    "비밀번호를 저장하지 않으려면 --headless 없이 실행해 직접 로그인해주세요."
                )
            else:
                _prefill_login_id(page)
                common.safe_print("[gmarket] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[gmarket] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_id, order_option))
    finally:
        page.close()
