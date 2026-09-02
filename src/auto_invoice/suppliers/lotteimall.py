"""롯데아이몰(LOTTE iMall / 롯데홈쇼핑) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.lotteimall.com/mypage/getOrderDtlInfo.lotte?ord_no=<주문번호>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고 ord_no만
  있으면 되도록 만들었다 - 실제로 다른 쿼리스트링 없이 ord_no 하나만 붙여도
  정상적으로 주문상세 페이지가 뜨는 것을 확인했다.)
- 로그인이 안 되어 있으면 https://www.lotteimall.com/member/login/forward.LCLoginMem.lotte
  로 리다이렉트된다. 로그인 폼 셀렉터: 아이디 "#login_id", 비밀번호 "#password".
  다만 **세션이 만료된 뒤에는 로그인 페이지가 아니라 메인 화면(/main/viewMain)으로
  튕긴다**(2026-08-31 실측 - 주문상세도, 마이페이지 주문목록도 그렇다). 그래서
  로그인 판정에 그 주소도 같이 본다. 예전에는 이걸 몰라서 세션이 끊긴 실행에서
  롯데아이몰 주문이 전부 '배송추적 링크를 찾지 못했습니다' 실패로 쌓였다.
- LOTTEIMALL_ID/LOTTEIMALL_PW 환경변수가 있으면 세션 만료 시 사람 개입 없이 완전
  자동으로 재로그인한다(롯데온/SSG/패션플러스와 동일한 방식). 로그인 페이지를
  실측해 확인한 것(2026-08-28):
  - 폼은 #frmLoginMem 하나이고, 로그인 버튼은 <a class="btn_login"> 이다.
    같은 클래스의 버튼이 "비회원 주문/조회" 탭에도 하나 더 있고 그건 숨겨져
    있으므로, 반드시 :visible 로 걸러서 회원 탭 버튼을 눌러야 한다.
  - reCAPTCHA/Turnstile 같은 봇 확인 스크립트도, 키보드보안 iframe도 없다.
    대신 사이트 자체 캡차(#catpcha_view_area)가 있는데 평소에는 숨겨져 있고
    로그인 실패가 반복될 때만 나타난다 - 이게 떠 있으면 자동 로그인을 포기하고
    사람에게 넘긴다(억지로 뚫지 않는다).
  - 로그인 실패는 화면 문구가 아니라 alert()으로 알려준다(롯데온과 같다).
    Playwright는 핸들러가 없으면 alert을 조용히 닫아버려서 실패를 감지하지
    못하므로, dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다.
  - "headless로는 로그인 페이지가 HTTP 403"(2026-08-28)의 정체는 headless가
    아니라 **기본 UA(HeadlessChrome)** 였다 - 2026-09-01 실측: 같은 headless
    브라우저라도 일반 크롬 UA를 주면 로그인 페이지가 열리고, 로그인된 쿠키로도
    주문상세가 UA에 따라 열리거나(일반 UA) 메인으로 튕긴다(HeadlessChrome UA).
    그래서 조회 컨텍스트 자체에 일반 UA를 주고(CONTEXT_KWARGS), 세션이
    만료되면 같은 브라우저의 별도 컨텍스트에서 로그인 화면을 직접 열어
    로그인하고 쿠키를 옮긴다(포스티와 같은 구조, 창은 뜨지 않는다). 성공
    판정은 주소가 아니라 LOGIN_TKN 쿠키다 - 성공해도 SSO 처리 페이지
    (LCSSOLogin_proc)가 빈 화면인 채 주소가 안 바뀌는 경우가 있다.
  LOTTEIMALL_PW를 비워두면 예전처럼 아이디만 자동 입력하고 사람이 직접 로그인한다.
- 주문상세 페이지에 있는 "배송추적" 링크(onclick="fn_DeliveryTrace(ord_no, ord_dtl_sn, hsm)")를
  클릭하면 새 팝업 탭(DeliveryTrace.lotte)이 뜨고, 거기에 "송장 번호\t<번호>\t택배사\t<택배사명> (대표번호)"
  형태로 이미 렌더링되어 있다 - API를 직접 호출할 필요 없이 그 텍스트만 읽으면 된다.
  택배사명은 "씨제이대한통운"처럼 한글 음차 표기로 나온다("CJ" -> "씨제이", "대한통운"은
  그대로) - "대한통운"이 포함되어 있으면 매칭되도록 다른 어댑터와 동일한 정규화 규칙을 쓴다.
- 상품이 여러 개라 "배송추적" 링크가 여러 개 뜨는 경우, 샵마인 엑셀의 "주문옵션" 값으로
  어느 링크인지 특정할 수 있으면 그 링크만 클릭한다. 특정할 수 없으면(무신사/GSSHOP과
  동일한 안전 규칙) 전부 클릭해서 실제로 서로 다른 송장인지 비교하고, 다르면 사람이
  확인하도록 예외를 던진다.
- 아직 발송 전(주문접수/결제완료/상품준비중)이면 "배송추적" 링크 자체가 없다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

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

# 세션이 끊기면 로그인 페이지가 아니라 **로그아웃 주소를 거쳐 메인 화면으로**
# 튕긴다 (2026-08-31 실측: 주문상세 -> /member/goLogout.lotte -> /main/viewMain).
LOGIN_PATH_MARKER = "/member/login"
LOGGED_OUT_MARKER = "/member/goLogout"
MAIN_PAGE_MARKER = "/main/viewMain"

# 로그인 화면 직행 주소 - 세션이 만료되면 별도 컨텍스트에서 이 주소를 직접
# 연다 (리다이렉트로는 메인 화면으로 튕겨서 로그인 폼에 갈 수 없다).
LOGIN_URL = "https://www.lotteimall.com/member/login/forward.LCLoginMem.lotte"

# 기본 UA(HeadlessChrome)면 로그인 페이지가 403이고, 로그인된 쿠키로도
# 주문상세가 메인으로 튕긴다(모듈 docstring). 조회/로그인 컨텍스트 모두
# 일반 크롬 UA를 쓴다. 오케스트레이터가 CONTEXT_KWARGS를 조회 컨텍스트에
# 그대로 넘겨준다 (browser.get_context).
NORMAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
CONTEXT_KWARGS = {"user_agent": NORMAL_USER_AGENT}

LOGIN_ID_SELECTOR = "#login_id"
LOGIN_PW_SELECTOR = "#password"
# "비회원 주문/조회" 탭에도 같은 클래스의 버튼이 있고 그쪽은 숨겨져 있다 - :visible 필수.
LOGIN_BUTTON_SELECTOR = "#frmLoginMem a.btn_login:visible"
# 로그인 실패가 반복되면 나타나는 사이트 자체 캡차 (평소에는 숨겨져 있다).
# id 오타(catpcha)는 사이트 원본 그대로다.
CAPTCHA_SELECTOR = "#catpcha_view_area"

DOMAINS = {"lotteimall.com", "www.lotteimall.com"}
SITE_KEY = "lotteimall"

DEFAULT_COURIER = "택배"  # 팝업에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게

TRACKING_LINK_TEXT = "배송추적"
# "배송추적" 링크의 onclick="fn_DeliveryTrace('주문번호','상세번호','hsm')" -
# 사이트 JS(imall_orderList.js)는 이걸로 아래 주소의 팝업을 연다. 그 페이지는
# 서버가 그려주므로 팝업을 띄우는 대신 HTML만 받는다 (2026-09-02 실측:
# 팝업 열고 닫기 2~3초 -> 0.05초).
TRACE_ONCLICK_PATTERN = re.compile(r"fn_DeliveryTrace\('([^']*)',\s*'([^']*)',\s*'([^']*)'")
TRACE_URL = ("https://www.lotteimall.com/mypage/DeliveryTrace.lotte"
             "?ord_no={ord_no}&ord_dtl_no={ord_dtl_no}&use_sct_cd=EC&hsm={hsm}")
TRACKING_PATTERN = re.compile(r"송장\s*번호\s+([0-9][0-9\-]{5,})")
COURIER_PATTERN = re.compile(r"택배사\s+([^\n(]+)")
NOT_YET_PATTERNS = ["주문접수", "결제완료", "상품준비중"]


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ord_no")
    if not values:
        raise ParseError(f"URL에서 ord_no 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _url_needs_login(url: str) -> bool:
    return (LOGIN_PATH_MARKER in url or LOGGED_OUT_MARKER in url or MAIN_PAGE_MARKER in url)


def _looks_like_login_page(page) -> bool:
    """로그인이 필요한 상태인지 본다.

    이 사이트는 세션이 끊기면 로그인 페이지로 가는 게 아니라 **로그아웃 주소를
    거쳐 메인 화면으로 튕긴다**(2026-08-31 실측 - 주문상세도 마이페이지
    주문목록도 그렇다). 그걸 로그인 필요로 보지 않으면 주문마다 '배송추적
    링크를 찾지 못했습니다' 파싱 실패가 쌓이고, 사람은 어댑터가 깨진 줄 알게
    된다. 실제로 2026-08-31 실행에서 롯데아이몰 주문이 그렇게 처리됐다.

    비밀번호 입력창 존재는 보지 않는다. 로그인 경로가 /member/login 으로 뚜렷해서
    주소만으로 충분하고, headless에서는 그 페이지가 403 Forbidden으로 와서
    입력창이 아예 없기 때문이다(입력창을 요구하면 두 번째 주문부터 로그인
    안내 대신 파싱 실패가 난다 - 2026-08-31 실측).
    """
    return common.looks_like_login_page(page, _url_needs_login, needs_password=False)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("LOTTEIMALL_ID"))


def _captcha_visible(page) -> bool:
    """사이트 자체 보안문자(캡차)가 화면에 떠 있는지 본다.

    평소에는 숨겨져 있고 로그인 실패가 반복될 때만 나타난다. 떠 있으면 자동
    로그인을 시도하지 않고 사람에게 넘긴다.
    """
    locator = page.locator(CAPTCHA_SELECTOR)
    if locator.count() == 0:
        return False
    try:
        return locator.first.is_visible()
    except Exception:
        return False


def _auto_login(context: BrowserContext) -> bool:
    """LOTTEIMALL_ID/LOTTEIMALL_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    같은 브라우저의 **별도 컨텍스트**에서 로그인 화면(LOGIN_URL)을 직접 열어
    로그인하고, 쿠키를 원래 컨텍스트로 옮긴다(포스티와 같은 구조). 세션이
    만료되면 주문상세가 로그인 폼이 아니라 메인 화면으로 튕기기 때문에, 지금
    보고 있는 페이지에서는 로그인할 수 없다.

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 안내(headless)
    또는 수동 로그인(창 모드)으로 넘어간다.

    롯데온 어댑터와 같은 패턴으로, 로그인 실패는 화면 문구가 아니라 alert()으로
    오기 때문에 dialog 핸들러로 그 문구를 받아 실패 사유째로 올린다. 성공
    판정은 주소가 아니라 LOGIN_TKN 쿠키다(모듈 docstring - SSO 처리 페이지가
    빈 화면으로 남는 경우가 있다).
    """
    login_id = os.environ.get("LOTTEIMALL_ID")
    login_pw = os.environ.get("LOTTEIMALL_PW")
    if not login_id or not login_pw:
        return False

    browser = context.browser
    if browser is None:
        raise BlockedError("롯데아이몰 로그인용 브라우저를 찾지 못했습니다.")

    login_context = browser.new_context(
        user_agent=NORMAL_USER_AGENT, viewport=browser_mod.DESKTOP_VIEWPORT,
        locale="ko-KR", timezone_id="Asia/Seoul")
    try:
        page = login_context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if page.locator(LOGIN_ID_SELECTOR).count() == 0:
            raise BlockedError(f"롯데아이몰 로그인 화면이 열리지 않았습니다 (주소={page.url}).")
        if _captcha_visible(page):
            raise BlockedError(
                "롯데아이몰이 보안문자(캡차)를 요구하고 있어 자동 로그인을 할 수 없습니다 "
                "- --headless 없이 실행해 직접 로그인해주세요."
            )

        alerts: list[str] = []

        def _on_dialog(dialog) -> None:
            alerts.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", _on_dialog)
        try:
            page.fill(LOGIN_ID_SELECTOR, login_id)
            page.fill(LOGIN_PW_SELECTOR, login_pw)
            page.locator(LOGIN_BUTTON_SELECTOR).first.click()

            elapsed_ms = 0
            while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
                page.wait_for_timeout(1500)
                elapsed_ms += 1500
                cookies = login_context.cookies()
                if any(c.get("name") == "LOGIN_TKN" for c in cookies):
                    context.add_cookies(cookies)
                    return True
                if alerts:
                    raise BlockedError(f"롯데아이몰 자동 로그인이 거부됐습니다: {alerts[0].strip()}")

            if _captcha_visible(page):
                raise BlockedError(
                    "롯데아이몰이 로그인 도중 보안문자(캡차)를 요구했습니다 "
                    "- --headless 없이 실행해 직접 로그인해주세요."
                )
            raise BlockedError("롯데아이몰 자동 로그인 후에도 로그인이 확인되지 않습니다.")
        finally:
            page.remove_listener("dialog", _on_dialog)
    finally:
        try:
            login_context.close()
        except Exception:  # noqa: BLE001 - 컨텍스트를 못 닫아도 결과에 영향은 없다
            pass


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _scrape_popup(popup) -> tuple[str, str]:
    popup.wait_for_load_state("domcontentloaded")
    # 팝업에 송장번호가 뜰 때까지만 기다린다 - 예전에는 무조건 1초를 잤다.
    # 끝내 안 뜨면 예전과 같은 1초를 채우고 아래에서 ParseError로 넘어간다.
    body_text = common.wait_for_match(
        popup, lambda: popup.inner_text("body"), TRACKING_PATTERN, timeout_ms=1000)

    return _parse_trace_text(body_text)


def _trace_page_text(html: str) -> str:
    """팝업 HTML을 화면 텍스트처럼 - 칸/줄 경계를 줄바꿈으로 바꿔 기존 패턴을 그대로 쓴다."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    text = re.sub(r"</(td|th|tr|p|div|li|dd|dt)>|<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())


def _click_tracking_link(context: BrowserContext, link) -> tuple[str, str]:
    """링크의 onclick에서 팝업 주소를 만들어 HTML만 받는다. 형식이 다르면 예전처럼 팝업을 연다."""
    match = TRACE_ONCLICK_PATTERN.search(link.get_attribute("onclick") or "")
    if match:
        ord_no, ord_dtl_no, hsm = match.groups()
        html = context.request.get(TRACE_URL.format(
            ord_no=ord_no.replace("-", ""), ord_dtl_no=ord_dtl_no, hsm=hsm)).text()
        return _parse_trace_text(_trace_page_text(html))

    with context.expect_page(timeout=10000) as popup_info:
        link.click()
    popup = popup_info.value
    try:
        return _scrape_popup(popup)
    finally:
        popup.close()


def _parse_trace_text(body_text: str) -> tuple[str, str]:
    tracking_match = TRACKING_PATTERN.search(body_text)
    if not tracking_match:
        raise ParseError("배송추적 팝업에서 송장번호를 찾지 못했습니다.")
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))
    courier_match = COURIER_PATTERN.search(body_text)
    courier = common.normalize_courier(courier_match.group(1).strip()) if courier_match else DEFAULT_COURIER
    return tracking_no, courier


def _select_link_index_by_order_option(body_text: str, count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 "배송추적" 링크 근처(상품명/옵션은 그 앞에
    나온다) 텍스트에만 유일하게 나타나면 그 링크의 인덱스를 쓴다. 0개 또는 2개 이상
    매칭되면 None - 호출자가 전부 클릭해서 비교하는 방식으로 넘어간다."""
    if count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    positions = [m.start() for m in re.finditer(TRACKING_LINK_TEXT, body_text)]
    candidates = []
    prev_end = 0
    for idx, pos in enumerate(positions):
        window = body_text[max(prev_end, pos - 500) : pos]
        if target in normalize_option(window):
            candidates.append(idx)
        prev_end = pos
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(
    context: BrowserContext, page, order_no: str, order_option: str | None
) -> TrackingResult:
    links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
    count = links.count()

    if count == 0:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"화면에서 배송추적 버튼을 찾지 못했습니다 (주문번호={order_no}).")

    if count == 1:
        tracking_no, courier = _click_tracking_link(context, links.first)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    body_text = page.inner_text("body")
    matched_idx = _select_link_index_by_order_option(body_text, count, order_option)
    if matched_idx is not None:
        tracking_no, courier = _click_tracking_link(context, links.nth(matched_idx))
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 클릭해서 실제로 서로 다른 송장인지 확인한다
    # (무신사/GSSHOP 어댑터와 동일한 안전 규칙).
    results = [_click_tracking_link(context, links.nth(i)) for i in range(count)]
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

        if _looks_like_login_page(page):
            # 세션이 만료되면 로그인 폼이 아니라 메인 화면으로 튕기므로, 이
            # 페이지에서는 로그인할 수 없다 - 별도 컨텍스트에서 로그인 화면을
            # 직접 열어 로그인하고 쿠키만 받아온다 (_auto_login).
            if _auto_login(context):
                common.safe_print("[lotteimall] 로그인 세션이 없어 자동 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "롯데아이몰 로그인이 필요하지만 LOTTEIMALL_ID/LOTTEIMALL_PW가 없습니다. "
                    ".env에 추가하거나 --headless 없이 실행해 직접 로그인해주세요."
                )
            else:
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                _prefill_login_id(page)
                common.safe_print("[lotteimall] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[lotteimall] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()
