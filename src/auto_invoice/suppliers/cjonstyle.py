"""CJ온스타일(base.cjonstyle.com) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://base.cjonstyle.com/p/myzone/orderInfo/<주문번호>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고, 경로의
  마지막 세그먼트를 주문번호로 쓴다.)
- 로그인이 안 되어 있을 때, 주문상세(orderInfo) URL로 직접 들어가면 다른
  사이트들과 달리 로그인 페이지로 리다이렉트되지 않고 조용히 홈
  (display.cjonstyle.com)으로 리다이렉트된다 - 그래서 주문상세 URL 이동
  결과만으로는 로그인 필요 여부를 판단할 수 없고, 이동 후 URL이 여전히
  "/p/myzone/" 경로인지로 판단한다(_looks_authenticated). 로그인이 필요하면
  로그인 페이지로 확실히 리다이렉트되는 주문목록(orderList) URL을 거쳐서
  로그인을 진행한다. 그 URL은 https://base.cjonstyle.com/p/page/account/login?...
  으로 리다이렉트된다. 로그인 폼 셀렉터: 아이디 "#id_input", 비밀번호
  "#password_input", 로그인 버튼 "#loginSubmit".
- 로그인 폼에는 Cloudflare Turnstile("사람인지 확인")이 걸려 있고, 통과하지
  않으면 로그인 버튼을 눌러도 그대로 로그인 페이지에 머문다. 한동안 이걸
  이유로 자동 로그인이 불가능하다고 판정했었는데, 원인은 자동화 자체가 아니라
  **크롬을 누가 실행했는지**였다(2026-08-28 실측). Turnstile 토큰
  (input[name='cf-turnstile-response']) 이 차는지로 갈렸다:
    * 번들 Chromium, 또는 Playwright가 띄운 진짜 크롬(현대몰을 통과시킨
      real_chrome_context) -> navigator.webdriver=true, 30초를 기다려도 토큰이
      빈 값(사람이 체크박스를 눌러도 "확인 실패")
    * 크롬을 우리가 평범하게 실행하고 --remote-debugging-port에
      connect_over_cdp로 붙기 -> navigator.webdriver=false, **3초 만에 토큰이
      저절로 채워짐**(사람이 누를 것도 없다)
  그래서 로그인만 browser.real_chrome_cdp_context()로 띄운 크롬 창에서 하고,
  성공하면 쿠키를 원래 컨텍스트로 옮겨 조회는 지금까지처럼 이어간다(현대몰과
  같은 "로그인만 별도 컨텍스트, 쿠키만 이식" 구조). 사람이 타이핑할 일은 없다.
  CJONSTYLE_PW가 비어 있으면 예전처럼 아이디만 자동 입력하고 사람이 직접
  로그인하는 경로로 넘어간다. 로그인 세션은 storage_state(쿠키)로 저장되므로
  다음 실행부터는 재로그인 없이 바로 조회된다.
  로그인 여부 판정은 '로그인 페이지가 아니면 성공' 같은 소극 판정이 아니라
  로그인된 화면에만 뜨는 "로그아웃" 표시로 확정한다(_login_check_result) -
  소극 판정은 리다이렉트가 늦으면 만료된 세션을 성공으로 오판해 만료된
  쿠키를 이식했다(2026-09-02 실행 실패의 원인).
- 로그인이 계속 실패하면 폼에 사이트 자체 캡차(#reCaptchaWarning /
  #turnstileWarning 경고 문구, "보안메뉴 (CAPTCHA)를 확인해 주세요")가 뜨는데,
  그때는 자동 로그인을 포기하고 사람에게 넘긴다(롯데아이몰과 같은 규칙).
- 주문상세 페이지의 "배송조회" 버튼을 클릭하면 새 탭이나 모달이 아니라 같은
  탭에서 https://base.cjonstyle.com/p/myzone/deliveryTracking/sheet?orderNo=
  <주문번호>&orderGSeq=<그룹순번>&orderWSeq=<배송순번> 으로 이동한다 (Hmall과
  동일한 패턴). 이 페이지는 API 호출 없이 서버에서 렌더링된 화면 텍스트에
  "송장번호" 다음 줄에 송장번호, "택배업체" 다음 줄에 "<택배사명> <전화번호>"
  형식으로 값이 그대로 노출되므로 화면 텍스트를 정규식으로 읽는다. 상단에
  "배송 상세정보가 존재하지않습니다 / 송장이 미등록되었거나..." 라는 문구가
  실제 송장번호가 있는 주문에도 뜨는 것을 확인했다 (택배사 실시간 배송이력
  조회가 안 될 때 뜨는 문구로 보이며, 송장번호/택배업체 필드 존재 여부와는
  무관하다) - 그래서 이 문구는 무시하고 "송장번호" 필드 존재 여부로만
  판단한다. 실제 확인한 값: 주문번호 20260826017435, 송장번호
  316726014614, 택배업체 "롯데택배 1588-2121" (정식 명칭 그대로 노출됨).
- 상품이 여러 개라 "배송조회" 버튼이 여러 개 뜨는 경우, 샵마인 엑셀의
  "주문옵션" 값으로 어느 버튼인지 특정할 수 있으면 그 버튼만 클릭한다.
  특정할 수 없으면(다른 어댑터와 동일한 안전 규칙) 전부 클릭해서 실제로 서로
  다른 송장인지 비교하고, 다르면 사람이 확인하도록 예외를 던진다. 클릭할
  때마다 같은 탭이 이동해버리므로, 다음 버튼을 클릭하기 전에 주문상세
  페이지로 다시 돌아간다(Hmall과 동일). 환불된 상품은 "배송조회"가 아니라
  "회수조회" 버튼이 뜨는 것을 확인했다 - exact text match라 자동으로
  제외된다.
- 아직 발송 전 상태 문구(NOT_YET_PATTERNS): 실제 미발송 주문 화면에서
  "상품준비중"을 확인했다 (이 상태에서는 "배송조회" 버튼 자체가 없다). 배송조회
  버튼을 눌렀는데도 결과 페이지에 "송장번호" 필드가 없는 경우도 같은 사유로
  간주해 TrackingNotAvailableYet으로 처리한다. 나머지 값들("결제완료",
  "배송준비중", "주문접수")은 다른 어댑터에서 흔히 보이는 값으로 추정해둔 것이라
  다르게 나오면 조정이 필요하다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

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

LOGIN_ID_SELECTOR = "#id_input"
LOGIN_PW_SELECTOR = "#password_input"
LOGIN_BUTTON_SELECTOR = "#loginSubmit"
# Cloudflare Turnstile이 통과되면 이 hidden input에 토큰이 채워진다.
TURNSTILE_TOKEN_SELECTOR = "input[name='cf-turnstile-response']"
# 로그인 실패가 반복될 때 사이트가 띄우는 경고들 - 여기까지 오면 사람에게 넘긴다.
CAPTCHA_WARNING_SELECTORS = ("#reCaptchaWarning", "#turnstileWarning")
# 아이디/비밀번호가 틀렸을 때 사이트가 문구를 넣는 영역.
LOGIN_MESSAGE_SELECTOR = ".loginEtcMessageArea"

DOMAINS = {"base.cjonstyle.com"}
SITE_KEY = "cjonstyle"

LOGIN_PATH = "/p/page/account/login"
MYZONE_PATH_PREFIX = "/p/myzone/"
# 비로그인 상태로 orderInfo(주문상세) URL에 직접 접근하면, 다른 사이트들처럼
# 로그인 페이지로 리다이렉트되는 게 아니라 조용히 홈(display.cjonstyle.com)으로
# 리다이렉트되는 것을 확인했다. 그래서 주문상세 URL만 봐서는 로그인 필요
# 여부를 판단할 수 없고, 로그인 페이지로 확실히 리다이렉트되는 이 URL을
# 거쳐서 로그인 여부를 확인/처리한다.
LOGIN_CHECK_URL = "https://base.cjonstyle.com/p/myzone/orderList?listType=ORDER&orderStatus=ALL"
DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분 (사람이 캡차+비번 입력)
LOGIN_CHECK_SETTLE_MS = 15 * 1000  # orderList가 로그인/마이존 어느 쪽인지 확정될 때까지 대기
LOGIN_FORM_RENDER_WAIT_MS = 5 * 1000  # 로그인 주소가 된 뒤 폼(입력창)이 그려질 때까지 대기
TURNSTILE_WAIT_TIMEOUT_MS = 30 * 1000  # "사람인지 확인" 토큰이 저절로 차기를 기다리는 최대 30초
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 제출 후 결과 대기 최대 30초
TRACKING_NAV_WAIT_TIMEOUT_MS = 5 * 1000  # 배송조회 클릭 후 페이지 이동 대기 최대 5초
TRACKING_TEXT_WAIT_TIMEOUT_MS = 5 * 1000  # 결과 페이지 본문에 값이 채워질 때까지 대기 최대 5초

TRACKING_LINK_TEXT = "배송조회"
TRACKING_URL_MARKER = "deliveryTracking/sheet"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문접수"]

TRACKING_NO_PATTERN = re.compile(r"송장번호\s*\n\s*([0-9]+)")
COURIER_PATTERN = re.compile(r"택배업체\s*\n\s*(\S+)")


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _looks_like_login_page(page: Page) -> bool:
    # 이 사이트는 자바스크립트로 화면을 넘긴다(실측: goto 직후에는 아직 주문상세
    # 주소) - 그래서 주소가 바뀌는지 잠깐 지켜본다.
    return common.looks_like_login_page(
        page, lambda url: LOGIN_PATH in urlparse(url).path,
        settle_ms=common.LOGIN_REDIRECT_SETTLE_MS)


def _looks_authenticated(page: Page) -> bool:
    """주문상세 URL로 이동한 뒤에도 그 경로(마이존)에 그대로 있는지 확인한다.

    비로그인 상태면 로그인 폼이 아니라 홈으로 조용히 리다이렉트되므로,
    로그인 폼 유무가 아니라 마이존 경로에 남아있는지로 판단해야 한다."""
    return MYZONE_PATH_PREFIX in urlparse(page.url).path


def _login_check_result(page: Page) -> str:
    """orderList로 이동한 결과가 어느 쪽인지 **확정될 때까지** 지켜본다.

    돌려주는 값: "authenticated" | "login" | ""(시간 안에 확정 못 함).

    goto 직후에는 주소가 아직 orderList(마이존 경로)라서 주소만으로는 '로그인돼
    있어서 남아 있는 것'과 '곧 로그인 페이지로 넘어갈 것'을 구분할 수 없다.
    예전에는 2.5초 자고 '로그인 페이지가 아니면 로그인된 것'으로 봤는데, 크롬
    첫 실행 직후처럼 리다이렉트가 그보다 늦으면 **만료된 세션을 로그인된 것으로
    오판**해 만료된 쿠키를 이식하고 성공을 보고했다(2026-09-02 아침 두 번의
    실행이 전부 이 경로로 실패했다). 그래서 소극 판정 대신, 로그인된 화면에만
    뜨는 헤더의 "로그아웃"이 보이거나 로그인 주소가 되거나 - 둘 중 하나가 될
    때까지 기다려서 확정한다 (로그인돼 있으면 0.5초 안에 "로그아웃"이 뜨는 것을
    확인했다).
    """
    elapsed_ms = 0
    while elapsed_ms < LOGIN_CHECK_SETTLE_MS:
        if LOGIN_PATH in urlparse(page.url).path:
            return "login"
        try:
            if _looks_authenticated(page) and "로그아웃" in page.inner_text("body"):
                return "authenticated"
        except Exception:  # noqa: BLE001 - 이동 중이면 본문을 못 읽는다, 다음 바퀴에 다시
            pass
        page.wait_for_timeout(500)
        elapsed_ms += 500
    return ""


def _wait_until_order_detail(page: Page, order_no: str) -> bool:
    """주문상세가 그려질 때까지만 기다리고, 로그인된 화면인지 알려준다.

    이 사이트는 비로그인이면 로그인 폼이 아니라 홈으로 조용히 넘어간다. 그래서
    예전에는 그 리다이렉트가 일어날 시간을 주려고 무조건 2.5초를 잤는데, 주문
    하나마다 2.5초면 100건에 4분이 통째로 사라진다.

    화면에 그 주문의 주문번호가 뜨면 '로그인돼 있다'와 '다 그려졌다'가 한 번에
    확인된다 - 밀려나는 홈에는 그 번호가 없다. 끝내 안 보이면 예전처럼 주소로
    판정한다(주문번호를 화면에 안 적는 화면이 있을 수 있어 폴백을 남긴다).
    """
    if common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS):
        return True
    return _looks_authenticated(page)


def _prefill_login_id(page: Page) -> None:
    """자동 로그인이 안 될 때(CJONSTYLE_PW 없음/캡차) 쓰는 폴백 - 아이디만 채운다.

    이 경로에서는 로그인 버튼을 자동으로 누르지 않는다 - 이 창(Playwright가
    띄운 브라우저)에서는 Turnstile이 통과되지 않아 눌러봐야 소용이 없고,
    어차피 사람이 비밀번호를 치는 중이다.
    """
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("CJONSTYLE_ID"))


def _turnstile_token(page: Page) -> str:
    try:
        return page.evaluate(
            "() => { const el = document.querySelector(\"input[name='cf-turnstile-response']\");"
            " return el ? el.value : ''; }"
        ) or ""
    except Exception:
        return ""


def _wait_for_turnstile(page: Page) -> bool:
    """"사람인지 확인"이 저절로 통과되기를 기다린다 (사람이 누를 것은 없다).

    real_chrome_cdp_context()로 띄운 창에서는 3초 안에 토큰이 찬다 - 30초를
    기다려도 안 차면 Turnstile이 이 브라우저를 자동화로 본 것이므로 포기한다.
    """
    elapsed_ms = 0
    while elapsed_ms < TURNSTILE_WAIT_TIMEOUT_MS:
        if _turnstile_token(page):
            return True
        page.wait_for_timeout(1000)
        elapsed_ms += 1000
    return False


def _captcha_warning_text(page: Page) -> str | None:
    """로그인 실패가 반복될 때 뜨는 사이트 캡차 경고 문구 (안 떴으면 None)."""
    for selector in CAPTCHA_WARNING_SELECTORS:
        locator = page.locator(selector)
        try:
            if locator.count() and locator.first.is_visible():
                return " ".join(locator.first.inner_text().split())
        except Exception:
            continue
    return None


def _login_message(page: Page) -> str:
    """아이디/비밀번호가 틀렸을 때 폼에 뜨는 안내 문구 (없으면 빈 문자열)."""
    locator = page.locator(LOGIN_MESSAGE_SELECTOR)
    try:
        if locator.count() == 0:
            return ""
        return " ".join(locator.first.inner_text().split())
    except Exception:
        return ""


def _auto_login(context: BrowserContext) -> bool:
    """CJONSTYLE_ID/CJONSTYLE_PW로 자동 로그인하고, 받은 쿠키를 원래 컨텍스트에 옮긴다.

    로그인은 우리가 직접 실행한 크롬 창(browser.real_chrome_cdp_context)에서만
    Turnstile을 통과한다 - 이유와 실측은 이 파일 맨 위 docstring 참고. 조회까지
    그 창에서 하지는 않고, 현대몰과 마찬가지로 쿠키만 원래 컨텍스트로 옮긴다.

    비밀번호가 없거나 캡차가 뜨면 False를 돌려주고 호출자가 기존 수동 로그인
    경로로 넘어간다.
    """
    login_id = os.environ.get("CJONSTYLE_ID")
    login_pw = os.environ.get("CJONSTYLE_PW")
    if not login_id or not login_pw:
        return False

    try:
        with browser_mod.real_chrome_cdp_context(SITE_KEY) as login_context:
            page = login_context.pages[0] if login_context.pages else login_context.new_page()

            page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
            check = _login_check_result(page)
            if check == "authenticated":
                # 이 프로필에 로그인이 남아 있으면 쿠키만 옮기고 끝낸다.
                context.add_cookies(login_context.cookies())
                return True
            if check != "login":
                common.safe_print("[cjonstyle] 로그인 여부를 확정하지 못했습니다 - 직접 로그인으로 넘어갑니다.")
                return False

            # _login_check_result는 주소가 로그인으로 바뀌는 즉시 돌아오므로,
            # 그 시점에는 폼이 아직 그려지는 중일 수 있다 - 입력창을 기다려준다.
            try:
                page.wait_for_selector(LOGIN_ID_SELECTOR, state="attached", timeout=LOGIN_FORM_RENDER_WAIT_MS)
            except Exception:  # noqa: BLE001 - 끝내 안 뜨면 아래에서 직접 로그인으로 넘어간다
                pass
            if page.locator(LOGIN_ID_SELECTOR).count() == 0:
                common.safe_print("[cjonstyle] 로그인 페이지에서 아이디 입력창을 찾지 못했습니다 - 직접 로그인으로 넘어갑니다.")
                return False

            if not _wait_for_turnstile(page):
                common.safe_print("[cjonstyle] '사람인지 확인'이 통과되지 않았습니다 - 직접 로그인으로 넘어갑니다.")
                return False

            page.locator(LOGIN_ID_SELECTOR).click()
            page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=80)
            page.wait_for_timeout(300)
            page.locator(LOGIN_PW_SELECTOR).click()
            page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=80)
            page.wait_for_timeout(600)
            page.locator(LOGIN_BUTTON_SELECTOR).click()

            elapsed_ms = 0
            while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
                # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
                # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
                page.wait_for_timeout(1500)
                if not _looks_like_login_page(page):
                    # 로그인 주소를 벗어났다고 세션 쿠키까지 다 깔린 것은 아니다 -
                    # 리다이렉트 체인 도중에 복사하면 덜 깔린 쿠키가 옮겨진다.
                    # orderList를 다시 열어 로그인을 확정한 뒤에 옮긴다.
                    page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
                    if _login_check_result(page) != "authenticated":
                        common.safe_print("[cjonstyle] 로그인 결과가 확인되지 않습니다 - 직접 로그인으로 넘어갑니다.")
                        return False
                    context.add_cookies(login_context.cookies())
                    common.safe_print("[cjonstyle] 자동 로그인에 성공했습니다.")
                    return True
                warning = _captcha_warning_text(page)
                if warning:
                    common.safe_print(f"[cjonstyle] 사이트가 캡차를 요구합니다({warning}) - 직접 로그인으로 넘어갑니다.")
                    return False
                message = _login_message(page)
                if message:
                    common.safe_print(f"[cjonstyle] 사이트가 로그인을 거부했습니다: {message}")
                    return False
                elapsed_ms += 1500

            common.safe_print("[cjonstyle] 자동 로그인 결과를 30초 안에 확인하지 못했습니다 - 직접 로그인으로 넘어갑니다.")
            return False
    except Exception as exc:
        common.safe_print(f"[cjonstyle] 자동 로그인 중 오류({exc}) - 직접 로그인으로 넘어갑니다.")
        return False


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _read_tracking_sheet(page: Page) -> str:
    """배송조회 결과 페이지의 본문을, 값이 채워진 뒤에 읽는다.

    이 페이지는 이동이 끝난 직후에는 "송장번호"/"택배업체" 자리가 아직 비어
    있고 1초쯤 뒤에 채워진다 - 바로 읽으면 송장이 있는 주문도 "아직 미발급"으로
    잘못 판단한다(2026-08-28 실측). 500ms 단위로 돌던 것을 100ms로 좁혔다 -
    채워지는 시점은 고정이 아니라서, 굵은 폴링은 평균 그 절반씩을 그냥 버린다.
    """
    elapsed_ms = 0
    body_text = page.inner_text("body")
    while "송장번호" not in body_text and elapsed_ms < TRACKING_TEXT_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(100)
        elapsed_ms += 100
        body_text = page.inner_text("body")
    return body_text


def _parse_tracking_page(body_text: str, order_no: str) -> tuple[str, str]:
    tracking_match = TRACKING_NO_PATTERN.search(body_text)
    if not tracking_match:
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    courier_match = COURIER_PATTERN.search(body_text)
    courier_raw = courier_match.group(1).strip() if courier_match else None
    courier = common.normalize_courier(courier_raw) if courier_raw else DEFAULT_COURIER

    return tracking_no, courier


def _click_tracking_link(page: Page, product_url: str, order_no: str, link,
                         *, return_to_detail: bool) -> tuple[str, str]:
    """"배송조회" 버튼은 새 탭이 아니라 같은 탭에서 결과 페이지로 이동한다.

    return_to_detail은 결과를 읽은 뒤 주문상세 페이지로 되돌아갈지다 - 다음에
    누를 버튼이 남았을 때만 필요하다. 버튼이 하나뿐인 보통 주문에서까지
    무조건 되돌아가면 주문마다 페이지 이동 하나가 통째로 낭비다
    (이 복귀를 없애고 폴링을 100ms로 좁혀 실측 주문당 5.4초 -> 2.9초).
    """
    link.click()
    elapsed_ms = 0
    while TRACKING_URL_MARKER not in page.url and elapsed_ms < TRACKING_NAV_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(100)
        elapsed_ms += 100
    if TRACKING_URL_MARKER not in page.url:
        raise ParseError("배송조회 클릭 후 결과 페이지로 이동하지 못했습니다.")

    result = _parse_tracking_page(_read_tracking_sheet(page), order_no)
    if return_to_detail:
        # 다음 버튼을 누르려면 주문상세가 다시 그려져 있어야 한다 - 주문번호가
        # 다시 보이면 그때가 다 그려진 때다(예전에는 여기서도 2.5초를 잤다).
        page.goto(product_url, wait_until="domcontentloaded")
        common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS)
    return result


def _select_link_index_by_order_option(body_text: str, count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 "배송조회" 버튼 근처(상품명/옵션은 그 앞에
    나온다) 텍스트에만 유일하게 나타나면 그 버튼의 인덱스를 쓴다. 0개 또는 2개 이상
    매칭되면 None - 호출자가 전부 클릭해서 비교하는 방식으로 넘어간다."""
    if count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    positions = [m.start() for m in re.finditer(re.escape(TRACKING_LINK_TEXT), body_text)]
    if len(positions) != count:
        return None  # 텍스트와 버튼 개수가 안 맞으면(예상치 못한 구조) 안전하게 포기
    candidates = []
    prev_end = 0
    for idx, pos in enumerate(positions):
        window = body_text[max(prev_end, pos - 500) : pos]
        if target in normalize_option(window):
            candidates.append(idx)
        prev_end = pos
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page: Page, product_url: str, order_no: str, order_option: str | None) -> TrackingResult:
    links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
    count = links.count()

    if count == 0:
        body_text = page.inner_text("body")
        normalized_body = normalize_option(body_text)
        if any(normalize_option(p) in normalized_body for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"화면에서 배송조회 버튼을 찾지 못했습니다 (주문번호={order_no}).")

    if count == 1:
        tracking_no, courier = _click_tracking_link(
            page, product_url, order_no, links.first, return_to_detail=False)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    body_text = page.inner_text("body")
    matched_idx = _select_link_index_by_order_option(body_text, count, order_option)
    if matched_idx is not None:
        tracking_no, courier = _click_tracking_link(
            page, product_url, order_no, links.nth(matched_idx), return_to_detail=False)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 클릭해서 실제로 서로 다른 송장인지 확인한다
    # (다른 어댑터와 동일한 안전 규칙). 클릭할 때마다 주문상세 페이지로 돌아오므로
    # (마지막 클릭 뒤에는 돌아올 필요가 없다), 매번 버튼을 새로 조회해야 한다
    # (이전 로케이터는 이동한 페이지 기준이라 재사용할 수 없다).
    results = []
    for i in range(count):
        fresh_links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
        results.append(_click_tracking_link(page, product_url, order_no, fresh_links.nth(i),
                                            return_to_detail=i < count - 1))

    distinct_tracking_nos = {r[0] for r in results}
    if len(distinct_tracking_nos) > 1:
        raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")

    tracking_no, courier = results[0]
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if not _wait_until_order_detail(page, order_no):
            # 자동 로그인은 자체 크롬 창을 띄우므로 headless 실행 중에도 쓸 수 있다.
            if _auto_login(context):
                page.goto(product_url, wait_until="domcontentloaded")
                if not _wait_until_order_detail(page, order_no):
                    raise BlockedError("자동 로그인 후에도 주문상세 페이지에 접근하지 못했습니다.")
            elif headless:
                raise BlockedError(
                    "CJ온스타일 로그인이 필요합니다. CJONSTYLE_PW를 넣거나, --headless 없이 실행해 수동으로 로그인해주세요."
                )
            else:
                # 주문상세 URL은 비로그인 시 로그인 폼이 아니라 홈으로 조용히
                # 리다이렉트되므로, 로그인 폼이 확실히 뜨는 URL을 거쳐서 로그인한다.
                page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
                if _looks_like_login_page(page):
                    _prefill_login_id(page)
                    common.safe_print("[cjonstyle] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호 입력, '사람인지 확인' 체크 후 로그인해주세요.")
                    common.safe_print("[cjonstyle] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                    if not _wait_for_manual_login(page):
                        raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
                page.goto(product_url, wait_until="domcontentloaded")
                if not _wait_until_order_detail(page, order_no):
                    raise BlockedError("로그인 후에도 주문상세 페이지에 접근하지 못했습니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, product_url, order_no, order_option))
    finally:
        page.close()
