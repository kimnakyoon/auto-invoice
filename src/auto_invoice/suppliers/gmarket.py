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
  2026-09-12 14:17 실행에서는 자동 로그인이 "성공"으로 찍힌 뒤 첫 주문부터
  "로그인 후에도 여전히 로그인 페이지"가 나 17건이 전부 실패했는데, 곧바로
  단독으로 다시 돌리니 2.4초 만에 통과했다(일시적 거부로 보이나 사유는 남지
  않았다). 그때의 _auto_login은 '비밀번호 칸이 없으면 성공'이라 중간 페이지
  (loginProc)에서 True를 돌려줬고, 거기서 로그인 폼으로 되돌아온 사유(alert)는
  잃은 채였다. 그래서 (1) 성공 판정은 주소가 로그인 흐름(/login)을 완전히
  벗어났을 때만 하고, 그 전에 alert이 온 채 로그인 폼에 서 있으면 그 문구로
  실패시키며, (2) 로그인 뒤 주문상세로 갔는데 다시 로그인 화면이면 한 번만
  더 로그인해 보고, (3) 그래도 안 되면 그때 주소·제목·화면 첫 줄을 사유에
  실어 다음에는 원인을 알 수 있게 한다(_page_summary).
- 로그인 실패는 화면 문구가 아니라 **alert()** 으로 알려준다 (실측: 없는
  아이디로 시도하면 "아이디 확인 후 다시 입력해 주세요."). 롯데온과 같은
  방식이라, dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다. 핸들러가
  없으면 Playwright가 alert을 조용히 닫아버려서 원인도 모른 채 대기 시간만
  다 쓰고 실패한다.
- 비밀번호를 저장하고 싶지 않은 경우를 위해, GMARKET_PW가 비어 있으면
  예전처럼 아이디만 자동 입력하고 사람이 직접 로그인하는 경로로 넘어간다
  (롯데온과 동일).
- **주문상세 JSON API로 화면 없이 답하기 (2026-09-02 실측).** 주문상세 화면이
  그려질 때 GET my.gmarket.co.kr/api/pay-detail/<장바구니번호> 를 부르고,
  거기 data.orderList[] 마다 displayOrderStatusName(배송중/배송준비중),
  orderDateTime, orderDelivery.tracking{trackingNumber, transCompanyName,
  transCompleteEstimateDate}, orderItem.itemName / itemOptionList 가 들어
  있다 - 배송조회 모달(tracking.gmarket.co.kr iframe)을 열 필요가 없다.
  context.request로 부르면 CDP 크롬의 쿠키를 그대로 써서 0.2초에 온다
  (화면 방식 0.9초 + 모달). 200/JSON이 아니면(세션 만료, 봇 확인) 예전
  화면 경로로 가서 로그인·봇 확인을 지나고, 그 뒤 API를 한 번 더 부른다.
  조회가 JSON GET 하나라 요청 간격도 4910과 같은 근거로 0.5~1.2초.
  화면 방식은 모달 텍스트의 상품명 끝 상품코드("... 네이비 15151448")까지
  송장 패턴에 걸려 송장이 하나뿐인 주문을 '여러 송장'으로 오판했다(2026-09-02
  실주문 2건 확인). JSON에는 송장 필드가 따로 있어 그런 오판이 없다.
"""

from __future__ import annotations

import contextlib
import html as html_mod
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    AlreadyInquired,
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    raise_if_delayed_any,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
    raise_if_cancelled_any,
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
# 주문상세 JSON의 주문상태(displayOrderStatusName)가 이 값이면 아직 발송 전이라
# 송장번호가 없는 게 정상이다 (옥션 NOT_YET_STATUSES와 같은 목록). 화면 텍스트용
# NOT_YET_PATTERNS와 따로 두는 이유: "결제완료"는 결제 정보 칸에도 찍히는 말이라
# 화면 전체 텍스트에 대고 쓰면 취소 주문까지 '미발급'으로 넘어간다. 2026-09-03
# 판매자 주문확인 전 주문(상태=결제완료)이 미발급 대신 실패로 기록된 것을 고친다.
NOT_YET_STATUSES = ["입금확인중", "결제완료", "배송준비중", "상품준비중", "결제확인중", "주문확인중"]
BOT_CHECK_PATTERNS = ["사람인지 확인", "봇(Bot)이란", "로봇이 아닙니다"]

# 조회를 우리가 직접 실행한 진짜 크롬(CDP)에서 한다는 표시 (orchestrator.py).
# 번들 크로미엄에서는 요청이 빠르면 "로봇이 아닙니다" 봇 확인이 떠서 한동안
# 요청 간격을 6~12초로 늘려 피했는데(2026-09-01), 옥션에서 확인한 대로 진짜
# 크롬(CDP)은 봇 확인이 아예 안 뜨고 떠도 저절로 풀리므로, 간격을 늘리는 대신
# 브라우저를 바꾸고 간격은 기본값(1.5~4초)으로 되돌렸다. 실행 중 크롬 창이
# 하나 뜬다 (옥션과 별도 프로필 auth/chrome_profile_gmarket).
WANTS_CDP_CHROME = True
# 요청 간격. 조회가 가벼운 JSON GET 하나라(맨 위 docstring) 4910과 같은
# 근거로 좁게 둔다 - 봇 확인이 뜨기 시작하면 이 값을 도로 넓히면 된다.
REQUEST_GAP = (0.5, 1.2)

# 주문상세 화면이 부르는 JSON API (맨 위 docstring).
PAY_DETAIL_API_URL = "https://my.gmarket.co.kr/api/pay-detail/{cart_no}"
KST = timezone(timedelta(hours=9))


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
    _settle_after_login(page)


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


def _page_summary(page) -> str:
    """실패 사유에 실을 '지금 무슨 화면인가' 한 줄 (주소·제목·본문 첫 줄)."""
    try:
        text = " ".join(page.inner_text("body").split())[:120]
    except Exception:  # noqa: BLE001 - 본문을 못 읽어도 주소·제목은 남긴다
        text = ""
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    return f"주소={page.url}, 제목={title!r}, 화면='{text}'"


def _settle_after_login(page) -> None:
    """리다이렉트 체인이 끝난 뒤 화면이 그려질 때까지 한 번 더 기다린다.

    뒤따르는 goto가 아직 도는 체인을 끊지 않게 하려는 것이다(맨 위 docstring).
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LOGIN_REDIRECT_SETTLE_MS)
    except Exception:  # noqa: BLE001 - 다음 goto가 어차피 다시 확인한다
        pass
    page.wait_for_timeout(500)


def _auto_login(page) -> bool:
    """GMARKET_ID/GMARKET_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    옥션(같은 이베이코리아 통합 로그인) 어댑터와 같은 패턴이지만, 지마켓은
    로그인 실패를 화면 문구가 아니라 alert()으로 알려주기 때문에 롯데온처럼
    dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다.

    성공은 주소가 로그인 흐름(Login → login/loginProc)을 완전히 벗어났을
    때만이다 - 중간 페이지에는 비밀번호 칸이 없어 '로그인 화면 아님'으로
    보이지만 거기서 다시 로그인 폼으로 되돌아올 수 있다(맨 위 docstring,
    2026-09-12). 그 사이 alert이 왔는데 로그인 폼에 그대로 서 있으면 그
    문구로 바로 실패시킨다. alert이 떴더라도 로그인 자체는 된 경우(비밀번호
    변경 안내 등)가 있어, 흐름을 벗어났으면 문구만 로그에 남기고 성공이다.

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
    started = time.monotonic()
    try:
        page.fill(LOGIN_ID_SELECTOR, login_id)
        page.fill(LOGIN_PW_SELECTOR, login_pw)
        page.click(LOGIN_BUTTON_SELECTOR)

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            elapsed_ms += 1500
            if not _is_login_flow_url(page.url):
                _settle_after_login(page)
                if alerts:
                    common.safe_print(f"[gmarket] 로그인 중 안내창이 떴습니다 (로그인은 됐습니다): {alerts[0].strip()}")
                return True
            if alerts and _looks_like_login_page(page):
                raise BlockedError(f"지마켓 자동 로그인이 거부됐습니다: {alerts[0].strip()}")

        if alerts:
            raise BlockedError(f"지마켓 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
        if _captcha_is_visible(page):
            raise BlockedError(
                "지마켓 로그인에 캡차가 요구됐습니다. --headless 없이 실행해 직접 로그인해주세요."
            )
        raise BlockedError(
            f"지마켓 자동 로그인 후 {time.monotonic() - started:.0f}초가 지나도 로그인 페이지에서 벗어나지 못했습니다 "
            f"(추가 본인인증을 요구받았을 수 있습니다 - {_page_summary(page)})."
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


def _kst_date(iso_text: str | None) -> date | None:
    """API의 ISO 시각("2026-09-01T22:39:38.380Z", UTC)을 한국 날짜로."""
    if not iso_text:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST).date()


def _fetch_pay_detail(context: BrowserContext, cart_no: str) -> dict | None:
    """주문상세 JSON. 세션이 없거나 봇 확인에 걸리면(200/JSON이 아니면) None."""
    try:
        response = context.request.get(PAY_DETAIL_API_URL.format(cart_no=cart_no))
        if response.status != 200 or "json" not in response.headers.get("content-type", ""):
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - API가 안 되면 화면 경로가 대신한다
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("orderList"):
        return None
    return data


def _option_text(order: dict) -> str:
    """상품명 + 옵션 값들 - 샵마인 '주문옵션'과 대조할 때 쓴다."""
    item = order.get("orderItem") or {}
    parts = [str(item.get("itemName") or "")]
    for option in item.get("itemOptionList") or []:
        parts.extend(str(option.get(key) or "") for key in ("itemOptionName", "value1", "value2", "value3"))
    return " ".join(parts)


def _answer_from_pay_detail(data: dict, order_id: str, order_option: str | None) -> TrackingResult:
    """주문상세 JSON으로 결론을 낸다 (화면 방식과 같은 규칙)."""
    orders = data.get("orderList") or []
    statuses = [str(o.get("displayOrderStatusName") or "").strip() for o in orders]
    order_date = _kst_date(data.get("payDate")) or _kst_date(orders[0].get("orderDateTime"))
    arrivals = []
    for o in orders:
        tracking = (o.get("orderDelivery") or {}).get("tracking") or {}
        arrival = _kst_date(tracking.get("transCompleteEstimateDate"))
        if arrival and arrival.isoformat() not in arrivals:
            arrivals.append(arrival.isoformat())
    note = " / ".join(f"도착예정 {a}" for a in arrivals) or None

    def fetch() -> TrackingResult:
        raise_if_cancelled_any(statuses, order_id)
        shipped = []
        for o in orders:
            tracking = (o.get("orderDelivery") or {}).get("tracking") or {}
            number = re.sub(r"[^0-9]", "", str(tracking.get("trackingNumber") or ""))
            if number:
                shipped.append((o, number, str(tracking.get("transCompanyName") or "").strip()))
        if not shipped:
            if any(p in status for status in statuses for p in NOT_YET_STATUSES):
                raise TrackingNotAvailableYet(f"아직 발송 전입니다 (orderId={order_id}, 상태={', '.join(statuses)}).")
            raise_if_delayed_any(statuses, order_id)
            raise ParseError(f"주문 응답에 송장번호가 없습니다 (orderId={order_id}, 상태={', '.join(statuses)}).")
        if len({number for _, number, _ in shipped}) > 1:
            target = normalize_option(order_option) if order_option else ""
            matched = [s for s in shipped if target and target in normalize_option(_option_text(s[0]))]
            if len({number for _, number, _ in matched}) != 1:
                # 상품별로 나눠 배송된 주문 - 어느 걸 써야 할지 확신할 수 없어 사람이 확인한다.
                raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (orderId={order_id}) - 상품별로 나눠 배송된 것으로 보입니다.")
            shipped = matched
        _, tracking_no, company = shipped[0]
        courier = common.normalize_courier(company) if company else DEFAULT_COURIER
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    return attach_order_date(order_date, fetch, delivery_note=note)


def _lookup_page(context: BrowserContext):
    page = _LOOKUP_PAGE.get(id(context))
    if page is None or page.is_closed():
        browser_mod.block_heavy_resources(context)
        page = context.new_page()
        _LOOKUP_PAGE[id(context)] = page
    return page


def _login_here(page, url: str) -> bool:
    """지금 서 있는 로그인 화면에서 로그인하고 url로 돌아온다 (자동이면 True).

    자동 로그인이 원래 주소가 아닌 곳에 떨어졌으면 그 화면을 로그에 남긴다 -
    로그인 뒤 다시 로그인 화면이 되는 일(2026-09-12)의 단서가 된다.
    """
    if _auto_login(page):
        common.safe_print("[gmarket] 로그인 세션이 없어 자동 로그인했습니다.")
        if page.url.split("?")[0].rstrip("/") != url.split("?")[0].rstrip("/"):
            common.safe_print(f"[gmarket] 로그인 뒤 원래 주소가 아닌 곳에 떨어졌습니다 ({_page_summary(page)}).")
        automatic = True
    else:
        # GMARKET_PW가 없거나 캡차가 요구된 경우 - 크롬 창이 항상 떠
        # 있으므로(CDP) 사람이 직접 로그인할 때까지 기다린다.
        _prefill_login_id(page)
        common.safe_print("[gmarket] 아이디는 자동으로 입력했습니다. 뜬 크롬 창에서 비밀번호를 입력하고 로그인해주세요.")
        common.safe_print("[gmarket] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
        if not _wait_for_manual_login(page):
            raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
        _wait_for_login_redirects(page)
        automatic = False
    common.goto_settled(page, url)
    if _looks_like_login_page(page):
        # 로그인 쿠키가 아직 다 안 붙었을 수 있다 - 잠깐 뒤 한 번만 더 가본다.
        page.wait_for_timeout(2000)
        common.goto_settled(page, url)
    return automatic


def _open_logged_in(page, url: str) -> None:
    """url을 열고, 봇 확인·로그인이 끼어들면 지나간 뒤 다시 url에 선다.

    조회(get_tracking)와 문의(post_inquiry)가 같이 쓴다. 자동 로그인 뒤에도
    로그인 페이지면 한 번만 더 로그인해 보고(2026-09-12 실행에서 첫 로그인이
    '성공'으로 찍힌 뒤 바로 로그인 화면으로 되돌아왔고, 단독으로 다시 하니
    통과했다), 그래도 로그인 페이지면 그때 화면 요약을 실어 BlockedError.
    """
    page.goto(url, wait_until="domcontentloaded")

    if _looks_like_bot_check(page):
        # 진짜 크롬(CDP)에서는 Turnstile이 몇 초 만에 저절로 풀린다(옥션과
        # 같은 원리). 창이 항상 떠 있으므로 headless 설정과 무관하게, 안
        # 풀리면 사람이 그 창에서 직접 통과할 수도 있다.
        common.safe_print("[gmarket] 봇 확인 화면이 떴습니다. 저절로 풀리기를 기다립니다 (뜬 크롬 창에서 직접 통과해도 됩니다).")
        if not _wait_for_bot_check_to_clear(page):
            raise BlockedError("봇 확인 대기 시간(3분)이 지났습니다. 통과 후 다시 실행해주세요.")
        page.goto(url, wait_until="domcontentloaded")

    if _looks_like_login_page(page):
        automatic = _login_here(page, url)
        if _looks_like_login_page(page) and automatic:
            common.safe_print("[gmarket] 로그인 뒤 주문상세를 열었는데 다시 로그인 화면입니다 - 한 번 더 로그인합니다.")
            page.wait_for_timeout(2000)
            _login_here(page, url)
        if _looks_like_login_page(page):
            raise BlockedError(f"로그인 후에도 여전히 로그인 페이지입니다 ({_page_summary(page)}).")


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_id = extract_order_id(product_url)

    # 화면을 열지 않고 JSON API로 먼저 답한다 (맨 위 docstring). 세션이 없거나
    # 봇 확인에 걸리면 None - 아래 화면 경로가 로그인/봇 확인을 처리한다.
    data = _fetch_pay_detail(context, order_id)
    if data is not None:
        return _answer_from_pay_detail(data, order_id, order_option)

    page = _lookup_page(context)
    _open_logged_in(page, product_url)

    # 로그인/봇 확인을 지났으니 API를 한 번 더 - 그래도 안 되면 화면에서 읽는다.
    data = _fetch_pay_detail(context, order_id)
    if data is not None:
        return _answer_from_pay_detail(data, order_id, order_option)

    # 주문상세는 자바스크립트로 그려진다 - 주문번호가 화면에 뜨면 다 그려진 것이다.
    common.wait_for_text(page, order_id)
    # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
    return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_id, order_option))


# --------------------------------------------------------------------------
# 1:1 문의 (판매자 문의) - inquiry.py가 부른다
# --------------------------------------------------------------------------
# 화면 순서 (2026-09-08 실측): 주문상세 [문의하기](button "문의하기") → 레이어의
#   [판매자 문의] → 같은 페이지 위 iframe#popLayerIframe 에
#   diary2.gmarket.co.kr/Popup/GoodsFAQWrite?cust_no=<암호화>&contr_no=<주문번호>
#   &l_top_gd_no=<상품번호>&w_no=&oversea_chk=N&is_my=true 가 뜬다 (cust_no가
#   열 때마다 바뀌는 값이라 주소를 직접 만들 수는 없다 - 화면을 거친다). 폼:
#   문의종류 radio[name=kind_in] (K2 상품 / K4 배송 / K18 취소 / K17 반품·취소 /
#   K19 교환 / K7 기타), #txt_email(회원 메일이 미리 채워짐), #txt_title(한글
#   19자까지), #ta_content, #mysecretyn(비밀글), [문의하기] 링크 = fn_write()
#   → confirm("문의 하시겠습니까?") → form POST Popup/SaveGoodsFAQ → 그 응답
#   스크립트가 alert("문의가 정상적으로 등록되었습니다. ...")를 띄우고 레이어를
#   닫는다. 확인/완료가 다 dialog라 핸들러로 받는다(로그인과 같은 사정).
#   숨은 input contr_no(주문번호)·gd_no(상품번호)로 폼이 이 주문에 붙었는지 본다.
# 연속 등록 제한 (2026-09-08 실측): 등록 직후 또 등록하면 SaveGoodsFAQ 응답이
#   alert("자동입력 방지를 위해 일시적으로 등록이 제한됩니다. 잠시 후 다시
#   등록해주세요.")로 거부한다. 간격을 40초로 낮추고 5초마다 다시 시도한 실측
#   (4건 연속): 앞 등록 뒤 42·48~49·55~56초는 전부 거부, 61~62초는 전부 통과
#   (시각은 시도가 끝난 뒤 잰 것이라 실제 제출은 그보다 1~2초 앞이다) - 즉
#   **마지막으로 등록된 때부터 60초**다. 거부된 시도가 제한 시간을 늘리지는
#   않는다(거부 세 번 뒤 그대로 통과). 그래서 앞 등록으로부터 INQUIRY_GAP_SEC
#   (60초 + 여유)을 기다린 뒤 등록하고, 그래도 거부되면(사람이 그 사이 직접
#   남겼거나 서버 시각이 다를 때) INQUIRY_RETRY_GAP_SEC마다 폼을 다시 열어
#   시도한다. 걸린 시간은 로그에 "앞 등록 뒤 N초"로 남긴다.
#   폼 열기·채우기(~0.8초)는 이 대기 '앞'에서 미리 한다(_prepare_inquiry_form) -
#   폼 여는 것 자체는 제한에 안 걸리므로 그 시간이 60초 대기에 흡수되고, 대기가
#   끝나면 [문의하기]만 눌러 제출이 제한 경계에 딱 맞는다. 대기 사이에 폼이
#   사라지면(_form_ready) 다시 연다. 제출하면 iframe이 SaveGoodsFAQ로 넘어가
#   폼이 없어지므로 재시도 때도 새로 연다.
# 문의내역 확인: 문의내역 화면(diary2/MYBBS/MyInqueryList3)이 쓰는
#   POST MyBBS/MyInqueryPage?startDate=&endDate=&ResponseStat=&PageNo=1
#   &SearchKind=T&SearchText=<검색어> 가 목록을 HTML 조각으로 준다(제목 검색,
#   최신순, 없으면 "문의하신 내역이 없습니다."). 줄마다 GetInquiryDetail(this,
#   '<writeNo>','배송'), text__status(접수완료/답변완료), text__item(상품명),
#   text__subject(제목), box__date(YYYY-MM-DD). 목록에는 주문번호가 없고 상세
#   (GET MyBBS/MyInquiryDetail?WriteNo=..&ViewAddQna=true)에 상품번호가 있다 -
#   주문상세 API(pay-detail)의 orderItem.itemNo와 같은 값이라 그것으로 이 주문의
#   문의인지 맞춘다(같은 수령인 주문이 둘이면 제목이 같다). 같은 근거로 등록
#   전에 이 주문의 같은 문의가 이미 있는지(사람이 직접 남긴 것) 보고, 있으면
#   AlreadyInquired로 넘긴다 - 2026-09-08 실행 전에 사용자가 세 건을 직접 남겨
#   두었고 장부에는 없었다.
INQUIRY_BUTTON = "문의하기"            # 주문상세의 버튼
INQUIRY_SELLER_BUTTON = "판매자 문의"  # [문의하기]를 누르면 뜨는 레이어의 버튼
INQUIRY_FORM_IFRAME = 'iframe[src*="Popup/GoodsFAQWrite"]'
INQUIRY_SAVE_URL_MARK = "Popup/SaveGoodsFAQ"
INQUIRY_KIND_RADIO = 'input[name="kind_in"][value="K4"]'   # 배송
INQUIRY_KIND_NAME = "배송"
INQUIRY_TITLE = "#txt_title"
INQUIRY_CONTENT = "#ta_content"
INQUIRY_SECRET = "#mysecretyn"
INQUIRY_ORDER_NO_INPUT = 'input[name="contr_no"]'
INQUIRY_ITEM_NO_INPUT = 'input[name="gd_no"]'
INQUIRY_SUBMIT = "문의하기"            # 폼 안의 링크 (javascript:fn_write())
INQUIRY_DONE_TEXT = "정상적으로 등록되었습니다"
INQUIRY_THROTTLED_TEXT = "일시적으로 등록이 제한"
INQUIRY_MESSAGE = "{name} 배송 언제 시작하나요?"
INQUIRY_STEP_WAIT_MS = 10000   # 클릭 뒤 다음 화면 요소·응답이 오기까지 최대
# 연속 등록 제한 (헤더 주석). 앞 등록 뒤 이만큼 지나야 다음 등록을 시도한다 -
# 실측 60초에 여유 2초. 우리가 재는 시각(완료 alert)이 서버의 등록 시각보다
# 늦으므로 그만큼은 이미 안전한 쪽이다.
INQUIRY_GAP_SEC = 62.0
INQUIRY_RETRY_GAP_SEC = 10.0          # 그래도 거부되면 이 간격으로 다시
INQUIRY_THROTTLE_WAIT_SEC = 240.0     # 거부가 이어질 때 한 건에 기다리는 최대
INQUIRY_LIST_API = ("https://diary2.gmarket.co.kr/MyBBS/MyInqueryPage"
                    "?startDate={start}&endDate={end}&ResponseStat=&PageNo=1&SearchKind=T&SearchText={text}")
INQUIRY_DETAIL_API = "https://diary2.gmarket.co.kr/MyBBS/MyInquiryDetail?WriteNo={write_no}&ViewAddQna=true"
INQUIRY_LIST_EMPTY_TEXT = "문의하신 내역이 없습니다"
INQUIRY_HISTORY_TRIES = 3             # 등록 뒤 목록에 아직 안 보이면 이만큼 다시 본다
INQUIRY_HISTORY_RETRY_GAP_SEC = 1.0
INQUIRY_HISTORY_LOOKBACK_DAYS = 30    # 주문일을 못 읽었을 때 '이미 남겼는지' 훑는 기간

# 이 프로세스에서 마지막으로 등록에 성공한 시각(monotonic). 연속 등록 제한은
# 계정 단위라 컨텍스트가 아니라 모듈에 둔다.
_last_inquiry_posted_at: float | None = None


def inquiry_message(recipient_name: str) -> str:
    return INQUIRY_MESSAGE.format(name=recipient_name.strip())


def _parse_inquiry_list(html: str) -> list[dict]:
    """MyInqueryPage 응답(HTML 조각)에서 줄마다 문의번호·종류·상태·제목·상품명·날짜."""
    items: list[dict] = []
    for m in re.finditer(r"GetInquiryDetail\(this,\s*'(\d+)',\s*'([^']*)'\)(.*?)</li>", html, re.S):
        block = m.group(3)

        def grab(cls: str) -> str:
            # 답변완료 줄은 class="text__status text__status--done"이라 class 값의 앞부분만 맞춘다.
            mm = re.search(rf'class="{cls}[^"]*"[^>]*>\s*(.*?)\s*</', block, re.S)
            return html_mod.unescape(mm.group(1)).strip() if mm else ""

        items.append({
            "write_no": m.group(1), "kind": m.group(2), "status": grab("text__status"),
            "item_name": grab("text__item"), "subject": grab("text__subject"), "date": grab("box__date"),
        })
    return items


def _fetch_inquiry_list(context: BrowserContext, text: str, start: date, end: date) -> list[dict] | None:
    """문의내역을 제목 검색어·기간으로 읽는다. 목록도 '없음' 문구도 아니면(세션 만료 등) None."""
    url = INQUIRY_LIST_API.format(start=start.isoformat(), end=end.isoformat(), text=quote(text))
    try:
        response = context.request.post(url)
        if response.status != 200:
            return None
        html = response.text()
    except Exception:  # noqa: BLE001 - 못 읽은 것으로 친다
        return None
    if INQUIRY_LIST_EMPTY_TEXT in html:
        return []
    return _parse_inquiry_list(html) or None


def _fetch_inquiry_item_no(context: BrowserContext, write_no: str) -> str | None:
    """문의 상세의 상품번호 (pay-detail의 orderItem.itemNo와 같은 값)."""
    try:
        response = context.request.get(INQUIRY_DETAIL_API.format(write_no=write_no))
        if response.status != 200:
            return None
        m = re.search(r'상품번호</span>\s*<span class="text__value">\s*(\d+)', response.text())
    except Exception:  # noqa: BLE001
        return None
    return m.group(1) if m else None


def _describe_listed(item: dict) -> str:
    return f"{item['status']} {item['date']} (문의번호 {item['write_no']}, {item['kind']})"


def _find_listed_inquiry(context: BrowserContext, recipient_name: str, message: str,
                         item_nos: set[str], start: date, end: date) -> dict | None:
    """문의내역에서 이 주문의 우리 문의(제목이 같고 상품번호가 맞는 것)를 찾는다.

    목록을 읽지 못하면 ParseError - 모르는 채로 등록하거나 성공이라 하지 않는다.
    """
    items = _fetch_inquiry_list(context, recipient_name, start, end)
    if items is None:
        raise ParseError("지마켓 문의내역 목록을 읽지 못했습니다 (세션이 끊겼을 수 있습니다).")
    for item in items:
        if item["subject"] != message:
            continue
        if _fetch_inquiry_item_no(context, item["write_no"]) in item_nos:
            return item
    return None


def _confirm_inquiry_listed(context: BrowserContext, recipient_name: str, message: str,
                            item_nos: set[str], today: date) -> str:
    """등록 뒤 문의내역에 오늘 자로 올라갔는지 확인하고 확인 문구를 돌려준다."""
    for attempt in range(1, INQUIRY_HISTORY_TRIES + 1):
        found = _find_listed_inquiry(context, recipient_name, message, item_nos, today, today)
        if found is not None:
            return _describe_listed(found)
        if attempt < INQUIRY_HISTORY_TRIES:
            common.sleep(INQUIRY_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"완료 문구는 받았지만 문의내역에서 확인되지 않았습니다. 다시 남기기 전에 지마켓 "
        f"나의G마켓 > 문의내역/쪽지함에서 '{message}'가 있는지 직접 확인해주세요.")


def _open_inquiry_form(page, product_url: str, cart_no: str, order_no: str, item_nos: set[str]):
    """주문상세 → [문의하기] → [판매자 문의] → 폼 iframe. 폼이 이 주문의 것인지 확인한다."""
    _open_logged_in(page, product_url)
    button = page.get_by_role("button", name=INQUIRY_BUTTON, exact=True)
    try:
        button.first.wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        if _looks_like_bot_check(page):
            raise BlockedError("지마켓 봇 확인 화면이 떴습니다 (주문상세). 뜬 크롬 창에서 통과한 뒤 다시 실행해주세요.") from None
        raise ParseError(f"주문상세에 [문의하기] 버튼이 없습니다 (cartNo={cart_no}, url={page.url}).") from None
    button.first.click()
    seller = page.get_by_text(INQUIRY_SELLER_BUTTON, exact=True)
    try:
        seller.first.wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError("[문의하기]를 눌렀는데 [판매자 문의] 버튼이 뜨지 않았습니다.") from None
    seller.first.click()
    form = page.frame_locator(INQUIRY_FORM_IFRAME)
    try:
        form.locator(INQUIRY_TITLE).wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError("[판매자 문의]를 눌렀는데 문의 폼(GoodsFAQWrite)이 뜨지 않았습니다.") from None
    form_order_no = form.locator(INQUIRY_ORDER_NO_INPUT).first.input_value()
    form_item_no = form.locator(INQUIRY_ITEM_NO_INPUT).first.input_value()
    if form_order_no != order_no or form_item_no not in item_nos:
        raise ParseError(f"문의 폼이 다른 주문에 붙었습니다 (폼 주문번호 {form_order_no}/상품 {form_item_no}, "
                         f"기대 {order_no}/{sorted(item_nos)}).")
    return form


def _fill_inquiry_form(form, message: str) -> None:
    """문의종류 [배송]·제목·내용·비밀글을 채운다 - [문의하기]는 누르지 않는다."""
    form.locator(INQUIRY_KIND_RADIO).check()
    form.locator(INQUIRY_TITLE).fill(message)
    form.locator(INQUIRY_CONTENT).fill(message)
    form.locator(INQUIRY_SECRET).check()
    if not form.locator(INQUIRY_KIND_RADIO).is_checked():
        raise ParseError(f"문의종류 [{INQUIRY_KIND_NAME}]이 선택되지 않았습니다.")
    if form.locator(INQUIRY_TITLE).input_value().strip() != message:
        raise ParseError("문의 제목이 입력되지 않았습니다.")
    if form.locator(INQUIRY_CONTENT).input_value().strip() != message:
        raise ParseError("문의 내용이 입력되지 않았습니다.")
    if not form.locator(INQUIRY_SECRET).is_checked():
        raise ParseError("[비밀글로 문의하기]가 체크되지 않았습니다.")


def _prepare_inquiry_form(page, product_url: str, cart_no: str, order_no: str,
                          item_nos: set[str], message: str):
    """문의 폼을 열고 채운 뒤 [문의하기] 직전 상태의 form을 돌려준다 - 아직 누르지 않는다.

    연속 등록 제한(60초)을 기다리는 동안 미리 불러 채워두려고 제출과 나눴다.
    대기가 끝나면 _click_inquiry_submit이 [문의하기]만 누른다.
    """
    form = _open_inquiry_form(page, product_url, cart_no, order_no, item_nos)
    _fill_inquiry_form(form, message)
    return form


def _form_ready(form, message: str) -> bool:
    """미리 열어둔 폼이 대기 사이에 사라지거나 값이 지워지지 않았는지 (제출 직전 점검)."""
    try:
        return (form.locator(INQUIRY_TITLE).input_value().strip() == message
                and form.locator(INQUIRY_KIND_RADIO).is_checked())
    except Exception:  # noqa: BLE001 - 프레임이 사라졌으면 다시 열어야 한다
        return False


def _click_inquiry_submit(page, form) -> tuple[str, list[tuple[str, str]]]:
    """채워둔 폼의 [문의하기]를 누른다. ("done"|"throttled"|"unknown", 뜬 dialog들)."""
    dialogs: list[tuple[str, str]] = []

    def _on_dialog(dialog) -> None:
        dialogs.append((dialog.type, dialog.message))
        dialog.accept()   # confirm("문의 하시겠습니까?")은 승인, 결과 alert은 닫는다

    def _has_alert() -> bool:
        return any(t == "alert" for t, _ in dialogs)

    page.on("dialog", _on_dialog)
    try:
        # fn_write()가 입력 검사에서 alert으로 멈추면 SaveGoodsFAQ 요청 자체가 없다 -
        # 응답을 못 받아도 여기서 올리지 않고 아래에서 dialog로 사유를 가린다.
        with contextlib.suppress(PlaywrightTimeoutError):
            with page.expect_response(lambda r: INQUIRY_SAVE_URL_MARK in r.url, timeout=INQUIRY_STEP_WAIT_MS):
                form.get_by_role("link", name=INQUIRY_SUBMIT, exact=True).click()
        # 결과 alert은 응답 스크립트가 띄운다 - 응답이 온 뒤 잠깐 더 기다린다.
        deadline = time.monotonic() + INQUIRY_STEP_WAIT_MS / 1000
        while not _has_alert() and time.monotonic() < deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener("dialog", _on_dialog)

    alerts = [m for t, m in dialogs if t == "alert"]
    if any(INQUIRY_DONE_TEXT in m for m in alerts):
        return "done", dialogs
    if any(INQUIRY_THROTTLED_TEXT in m for m in alerts):
        return "throttled", dialogs
    return "unknown", dialogs


def _wait_for_inquiry_gap() -> None:
    """앞 등록 뒤 INQUIRY_GAP_SEC이 지날 때까지 기다린다 (헤더 주석의 연속 등록 제한)."""
    if _last_inquiry_posted_at is None:
        return
    remaining = INQUIRY_GAP_SEC - (time.monotonic() - _last_inquiry_posted_at)
    if remaining > 0:
        common.safe_print(f"[gmarket] 연속 등록 제한 때문에 {remaining:.0f}초 기다린 뒤 다음 문의를 남깁니다.")
        common.sleep(remaining)


def _since_last_posted() -> str:
    if _last_inquiry_posted_at is None:
        return "이 실행의 첫 등록"
    return f"앞 등록 뒤 {time.monotonic() - _last_inquiry_posted_at:.0f}초"


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """판매자 문의(배송, 비밀글)를 남기고 완료 문구를 돌려준다.

    주문상세 API로 주문번호·상품번호·상태를 읽어 취소/품절 주문은 남기지 않고,
    문의내역에 이 주문의 같은 문의가 이미 있으면 AlreadyInquired로 넘긴다.
    등록은 연속 등록 제한을 지켜 시도하고, 완료 문구를 받은 뒤 문의내역에
    오늘 자로 올라갔는지까지 확인한다(_confirm_inquiry_listed). 어디서든
    어긋나면 ParseError/BlockedError - 남겼는지 불확실한 채로 성공이라 하지 않는다.
    """
    global _last_inquiry_posted_at
    cart_no = extract_order_id(product_url)
    message = inquiry_message(recipient_name)
    page = _lookup_page(context)

    data = _fetch_pay_detail(context, cart_no)
    if data is None:
        _open_logged_in(page, product_url)   # 세션 만료·봇 확인을 지나고 다시
        data = _fetch_pay_detail(context, cart_no)
    if data is None:
        raise ParseError(f"주문상세 API가 답하지 않아 문의를 남길 수 없습니다 (cartNo={cart_no}).")
    orders = data.get("orderList") or []
    statuses = [str(o.get("displayOrderStatusName") or "").strip() for o in orders]
    with contextlib.suppress(TrackingNotAvailableYet):   # '아직 준비 중'은 문의 대상 그 자체다
        raise_if_cancelled_any(statuses, cart_no)
    order_no = str(orders[0].get("orderNo") or "")
    item_nos = {str((o.get("orderItem") or {}).get("itemNo") or "") for o in orders} - {""}
    if not order_no or not item_nos:
        raise ParseError(f"주문상세 API에 주문번호/상품번호가 없습니다 (cartNo={cart_no}).")

    today = datetime.now(KST).date()
    order_date = (_kst_date(data.get("payDate")) or _kst_date(orders[0].get("orderDateTime"))
                  or today - timedelta(days=INQUIRY_HISTORY_LOOKBACK_DAYS))
    existing = _find_listed_inquiry(context, recipient_name, message, item_nos, order_date, today)
    if existing is not None:
        raise AlreadyInquired(f"문의내역에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

    # 폼을 대기 '앞'에서 열어 채운다 - 폼 열기(~0.8초)가 60초 대기에 흡수되고,
    # 대기가 끝나면 [문의하기]만 눌러 제출이 제한 경계에 딱 맞는다. 폼 여는 것
    # 자체는 제한에 안 걸린다(제한은 등록=SaveGoodsFAQ에만 걸린다).
    form = _prepare_inquiry_form(page, product_url, cart_no, order_no, item_nos, message)
    _wait_for_inquiry_gap()
    started = time.monotonic()
    while True:
        if not _form_ready(form, message):
            # 대기 사이에 폼이 사라졌거나(세션·레이어 타임아웃) 재시도라 새로 연다.
            form = _prepare_inquiry_form(page, product_url, cart_no, order_no, item_nos, message)
        outcome, dialogs = _click_inquiry_submit(page, form)
        if outcome == "done":
            break
        if outcome != "throttled":
            seen = " / ".join(f"{t}: {m}" for t, m in dialogs) or "(뜬 창 없음)"
            raise ParseError(f"[문의하기]를 눌렀는데 완료 문구가 오지 않았습니다 ({seen}).")
        waited = time.monotonic() - started
        if waited + INQUIRY_RETRY_GAP_SEC > INQUIRY_THROTTLE_WAIT_SEC:
            raise BlockedError(f"지마켓 연속 등록 제한이 {waited:.0f}초가 지나도 풀리지 않았습니다 "
                               f"({_since_last_posted()}). 잠시 뒤 다시 실행해주세요.")
        common.safe_print(f"[gmarket] 연속 등록 제한에 걸렸습니다 ({_since_last_posted()}). "
                          f"{INQUIRY_RETRY_GAP_SEC:.0f}초 뒤 다시 엽니다.")
        common.sleep(INQUIRY_RETRY_GAP_SEC)
        # 제출하면 iframe이 SaveGoodsFAQ로 넘어가 폼이 없다 - 다음 바퀴가 새로 연다.
    common.safe_print(f"[gmarket] 등록됐습니다 ({_since_last_posted()}).")
    _last_inquiry_posted_at = time.monotonic()
    listed = _confirm_inquiry_listed(context, recipient_name, message, item_nos, today)
    return f"문의가 정상적으로 등록되었습니다 · 문의내역 확인: {listed}"


# ---------------------------------------------------------------------------
# 문의 답변 확인 (inquiry_answers.py)
# 2026-09-11 실측: 문의 상세(MyInquiryDetail?WriteNo=..&ViewAddQna=true) HTML의
# ul.list__inquiry-history에 li.list-item--question(우리 문의)과 li.list-item--answer
# (판매자 답변, 여럿일 수 있음)가 있고 각각 text__subject·text__date("2026-09-11 오전
# 11:12:17")·text__content를 가진다. 목록의 상태는 text__status--done이 답변완료.
# ---------------------------------------------------------------------------
INQUIRY_DETAIL_ITEM = re.compile(r'<li class="list-item list-item--(question|answer)">(.*?)</li>', re.S)
INQUIRY_DETAIL_SUBJECT = re.compile(r'<p class="text__subject">\s*(.*?)\s*</p>', re.S)
INQUIRY_DETAIL_DATE = re.compile(r'<p class="text__date">\s*(.*?)\s*</p>', re.S)
INQUIRY_DETAIL_CONTENT = re.compile(r'<div class="text__content">(.*?)</div>', re.S)
INQUIRY_DETAIL_MARK = "list__inquiry-history"


def _fetch_inquiry_detail_html(context: BrowserContext, write_no: str) -> str | None:
    try:
        response = context.request.get(INQUIRY_DETAIL_API.format(write_no=write_no))
        if response.status != 200:
            return None
        html = response.text()
    except Exception:  # noqa: BLE001
        return None
    return html if INQUIRY_DETAIL_MARK in html else None


def _answer_from_detail(html: str, write_no: str) -> dict | None:
    question = None
    answers: list[tuple[str, str]] = []
    for kind, block in INQUIRY_DETAIL_ITEM.findall(html):
        d = INQUIRY_DETAIL_DATE.search(block)
        c = INQUIRY_DETAIL_CONTENT.search(block)
        when = html_mod.unescape(d.group(1)).strip() if d else ""
        text = common.html_to_text(c.group(1)) if c else ""
        if kind == "question" and question is None:
            subject = INQUIRY_DETAIL_SUBJECT.search(block)
            question = {"subject": html_mod.unescape(subject.group(1)).strip() if subject else "", "date": when}
        elif kind == "answer" and text:
            answers.append((when, text))
    if question is None:
        return None
    return {
        "inquiry_id": write_no,
        "subject": question["subject"],
        "state": "답변완료" if answers else "접수완료",
        "written_on": question["date"][:10],
        "answer": "\n\n".join(text for _, text in answers) or None,
        "answered_on": answers[-1][0][:10] if answers else None,
    }


def fetch_inquiry_answer(context: BrowserContext, product_url: str, recipient_name: str, *,
                         since: date, inquiry_id: str | None = None, headless: bool = False) -> dict | None:
    """이 주문에 남긴 판매자 문의의 상태·답변 - {inquiry_id, state, written_on, answer, answered_on}, 없으면 None.

    장부의 문의번호가 있으면 상세 한 번(제목이 우리 문구인지 확인)으로 끝낸다. 없으면
    등록 때처럼 주문상세 API로 상품번호를 읽어 문의내역(수령인 이름 검색)에서 맞춘다.
    세션이 끊겼으면 주문상세 화면을 열어 지나간다.
    """
    cart_no = extract_order_id(product_url)
    message = inquiry_message(recipient_name)
    if inquiry_id:
        html = _fetch_inquiry_detail_html(context, inquiry_id)
        if html is None:
            _open_logged_in(_lookup_page(context), product_url)
            html = _fetch_inquiry_detail_html(context, inquiry_id)
        found = _answer_from_detail(html, inquiry_id) if html else None
        if found is not None and found["subject"] == message:
            return found
    data = _fetch_pay_detail(context, cart_no)
    if data is None:
        _open_logged_in(_lookup_page(context), product_url)
        data = _fetch_pay_detail(context, cart_no)
    if data is None:
        raise BlockedError(f"주문상세 API가 답하지 않아 문의를 찾을 수 없습니다 (cartNo={cart_no}).")
    orders = data.get("orderList") or []
    item_nos = {str((o.get("orderItem") or {}).get("itemNo") or "") for o in orders} - {""}
    if not item_nos:
        raise ParseError(f"주문상세 API에 상품번호가 없습니다 (cartNo={cart_no}).")
    listed = _find_listed_inquiry(context, recipient_name, message, item_nos, since, datetime.now(KST).date())
    if listed is None:
        return None
    html = _fetch_inquiry_detail_html(context, listed["write_no"])
    found = _answer_from_detail(html, listed["write_no"]) if html else None
    if found is None:
        return {"inquiry_id": listed["write_no"], "state": listed["status"] or "접수완료",
                "written_on": listed["date"], "answer": None, "answered_on": None}
    return found
