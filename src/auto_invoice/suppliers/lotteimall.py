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

import contextlib
import html as html_mod
import os
import re
import time
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

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
    normalize_option,
    raise_if_cancelled,
    raise_if_cancelled_any,
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

# 주문당 상세 화면 1개를 여는 사이트. 기본 간격(1.5~4초)은 봇 확인이 잘 뜨는
# 사이트를 기준으로 잡은 값이라, 화면 하나 여는 데 1.2초쯤 걸리는 여기서는
# 조회 시간의 절반이 그냥 쉬는 시간이었다 (2026-09-04 실측: 롯데아이몰 6건
# 15.5초 중 순수 조회 7.5초). 네이버와 같은 간격으로 둔다 - 사람이 주문을
# 하나씩 눌러 보는 속도다.
REQUEST_GAP = (1.0, 2.0)


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


def _goto_logged_in(context: BrowserContext, page, url: str, headless: bool) -> None:
    """주소를 열고, 로그인이 끊겨 있으면 로그인한 뒤 다시 연다 (송장조회·문의 공용).

    세션이 만료되면 로그인 폼이 아니라 메인 화면으로 튕기므로, 이 페이지에서는
    로그인할 수 없다 - 별도 컨텍스트에서 로그인 화면을 직접 열어 로그인하고
    쿠키만 받아온다 (_auto_login). 비밀번호가 없으면 창 모드에서만 사람이 직접
    로그인한다.
    """
    page.goto(url, wait_until="domcontentloaded")
    if not _looks_like_login_page(page):
        return
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
    page.goto(url, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        _goto_logged_in(context, page, product_url, headless)

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()


# ---------------------------------------------------------------------------
# 1:1 문의 (배송/회수 > 배송문의) - 2026-09-08 실측
#
# 사용자가 정한 화면 순서: 주문상세 [1:1문의] > 문의 유형 분류 [배송/회수] >
# [배송문의] > 문의 제목·문의 내용에 "<수령인> 배송 언제 시작하나요?" > [등록].
#
# - 주문상세의 [1:1문의]는 onclick="fn_goInquireForm({ord_no, goods_no, ord_dtl_sn})"
#   이고, 그 함수는 location.href로 아래 INQUIRY_FORM_URL로 간다(새 탭 아님).
#   주문상세는 서버가 그려주므로 화면을 열지 않고 HTML만 받아 그 세 값과
#   상품별 진행상태(<div class="wrap_ing2 ...">상품준비중</div>)를 읽는다.
# - 문의 화면: 대분류 #cust_inq_mdl_tp_cd(1401=배송/회수)를 고르면
#   selectFaqSmallMenuAjaxList.lotte 로 소분류를 받아 #cust_inq_sml_tp_cd 에
#   채운다(140101=배송문의, 140102=회수문의). 주소에 ty_up_cd/ty_sub_cd를 주면
#   서버가 골라 놓기도 하지만 그때는 숨은 select(외부고객게시글사유코드)가
#   비어 사람이 고른 것과 달라지므로 사람처럼 change로 고른다.
# - [등록](#inquire_add, <img>)을 누르면 사이트 JS가 상품번호 확인
#   (getGoodsNoYn) -> 중복 문의 확인(getOrdDupInquireAjax: 같은 주문·상품·유형의
#   처리 중인 문의가 있으면 '중복 문의 알림' 레이어 #inquireDup_lypopup.open) ->
#   폼 POST insertInquire.lotte 순으로 간다. 확인창(confirm)은 없다. 중복 알림이
#   뜨면 등록하지 않고 AlreadyInquired로 넘긴다.
# - 상담내역(searchinquirePagingList.lotte?pageIdx=n, 기본 최근 1개월)은 줄마다
#   fn_goDetailLayer('문의번호','NEC','n','주문번호','상품번호') 링크에 제목이
#   붙어 있어(제목=우리 문구) 상세를 열 필요가 없다. 등록 전에 이 주문의
#   '배송 언제' 문의가 주문일 이후에 있으면 넘기고, 등록 뒤 오늘 자로 올라갔는지
#   확인한다. 이 주문의 주문일은 주문번호 앞 8자리(20260904H20839 -> 2026-09-04)다.
# ---------------------------------------------------------------------------
INQUIRY_LINK_PATTERN = re.compile(
    r"fn_goInquireForm\(\{ord_no:'([^']*)',\s*goods_no:'([^']*)',\s*ord_dtl_sn:'([^']*)'\}\)")
INQUIRY_STATUS_PATTERN = re.compile(r'<div class="wrap_ing2[^"]*">\s*([^<]*?)\s*<', re.S)
INQUIRY_FORM_URL = ("https://www.lotteimall.com/custcenter/getinquireForm.lotte"
                    "?ty_up_cd=&ty_sub_cd=&ord_no={ord_no}&goods_no={goods_no}&ord_dtl_sn={ord_dtl_sn}")
INQUIRY_FORM_MARKER = "/custcenter/getinquireForm"
INQUIRY_TYPE_SELECT = "#cust_inq_mdl_tp_cd"
INQUIRY_TYPE_CODE = "1401"            # 문의 유형 분류 [배송/회수]
INQUIRY_TYPE_LABEL = "배송/회수"
INQUIRY_SUBTYPE_SELECT = "#cust_inq_sml_tp_cd"
INQUIRY_SUBTYPE_CODE = "140101"       # [배송문의]
INQUIRY_SUBTYPE_LABEL = "배송문의"
INQUIRY_SUBTYPE_API = "selectFaqSmallMenuAjaxList"
INQUIRY_TITLE = "#accp_tit_nm"
INQUIRY_CONTENT = "#accp_cont"
INQUIRY_SUBMIT = "#inquire_add"
INQUIRY_DUP_POPUP = "#inquireDup_lypopup.open"
INQUIRY_POST_PATH = "/custcenter/insertInquire.lotte"
INQUIRY_TITLE_MAX = 200
INQUIRY_CONTENT_MAX = 2000
INQUIRY_STEP_WAIT_MS = 10000          # 화면 요소·소분류 응답·등록 뒤 화면 이동까지 최대
INQUIRY_ALLOWED_HOSTS = ("lotteimall.com",)

INQUIRY_LIST_URL = "https://www.lotteimall.com/mypage/searchinquirePagingList.lotte?pageIdx={page}"
INQUIRY_LIST_MARKER = "일대일 답변 목록"
INQUIRY_ROW_PATTERN = re.compile(r'<tr id="eventBBSQ_\d+">(.*?)</tr>', re.S)
INQUIRY_ROW_LINK = re.compile(
    r"fn_goDetailLayer\('(\d+)',\s*'[^']*',\s*'[^']*',\s*'([^']*)',\s*'([^']*)'\);?\"[^>]*>(.*?)</a>", re.S)
INQUIRY_ROW_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
INQUIRY_PAGE_ON = re.compile(r'class="on">\s*(\d+)\s*<')
INQUIRY_PAGE_LINK = re.compile(r"pageIdx=(\d+)")
INQUIRY_SAME_MARK = "배송 언제"       # 상담내역에서 '같은 문의'로 보는 표식 (주문번호와 함께)
INQUIRY_HISTORY_TRIES = 3             # 등록 뒤 목록에 아직 안 보이면 이만큼 다시 본다
INQUIRY_HISTORY_RETRY_GAP_SEC = 1.0
INQUIRY_HISTORY_MAX_PAGES = 5         # '이미 남겼는지' 훑는 상담내역 페이지 수

# 이 실행(컨텍스트)에서 이미 읽어둔 상담내역 {"rows": [...최신순], "pages": n, "last": n}.
# 한 배치의 주문들이 같은 목록을 보므로 주문마다 다시 받지 않는다(prepare_inquiries가 비운다).
_inquiry_rows_cache: dict[int, dict] = {}


def prepare_inquiries(context: BrowserContext, product_urls, headless: bool = False) -> None:
    """새 배치 - 앞 배치에서 읽어둔 상담내역 캐시를 비운다."""
    _inquiry_rows_cache.pop(id(context), None)


def _order_date_of(ord_no: str) -> date | None:
    """주문번호 앞 8자리가 주문일이다 (20260904H20839 -> 2026-09-04). 형식이 다르면 None."""
    try:
        return datetime.strptime(ord_no[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _get_html(context: BrowserContext, url: str) -> str | None:
    """브라우저 쿠키로 GET - 메인/로그인 화면으로 튕기거나 실패하면 None."""
    try:
        response = context.request.get(url)
    except Exception:  # noqa: BLE001 - 통신 실패는 '못 읽음'
        return None
    if not response.ok or _url_needs_login(response.url):
        return None
    return response.text()


def _abort_third_party(route) -> None:
    """문의 화면 전용 라우팅 - 롯데아이몰 밖 호스트(태그매니저·광고·픽셀)는 끊는다.

    페이지 라우팅이 걸리면 컨텍스트 공용 이미지 차단은 이 페이지에 적용되지
    않아 [등록] 버튼 이미지(image.lotteimall.com)가 열린다 - 일부러 그렇게 둔다.
    <img>가 안 뜨면 크기가 0이라 클릭할 수 없다.
    """
    host = urlparse(route.request.url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in INQUIRY_ALLOWED_HOSTS):
        route.continue_()
    else:
        route.abort()


def _order_for_inquiry(context: BrowserContext, product_url: str, ord_no: str,
                       headless: bool) -> dict:
    """주문상세 HTML에서 [1:1문의] 링크의 값(goods_no·ord_dtl_sn)을 읽고 취소/품절이면 올린다.

    화면 없이 HTML만 받는다(로그인이 끊겨 있으면 그때만 화면을 열어 로그인하고
    다시 받는다). '아직 준비 중'(TrackingNotAvailableYet)은 문의 대상 그 자체라
    지나간다. 페이지 전체 글자로 취소를 판정하지 않는다 - [주문취소] 버튼 글자가
    늘 있어서다 - 상품별 진행상태 칸만 본다.
    """
    html = _get_html(context, product_url)
    if html is None:
        page = context.new_page()
        try:
            _goto_logged_in(context, page, product_url, headless)
            html = page.content()
        finally:
            page.close()
    links = [m.groups() for m in INQUIRY_LINK_PATTERN.finditer(html)
             if m.group(1).replace("-", "") == ord_no]
    if not links:
        if ord_no not in html and ord_no[:8] not in html:
            raise ParseError(f"주문상세가 열리지 않았습니다 (주문번호={ord_no}).")
        raise ParseError(f"주문상세에 [1:1문의] 버튼이 없습니다 (주문번호={ord_no}).")
    statuses = [html_mod.unescape(s).strip() for s in INQUIRY_STATUS_PATTERN.findall(html)]
    with contextlib.suppress(TrackingNotAvailableYet):
        raise_if_cancelled_any(statuses, ord_no)
    _, goods_no, ord_dtl_sn = links[0]
    return {"ord_no": ord_no, "goods_no": goods_no, "ord_dtl_sn": ord_dtl_sn, "statuses": statuses}


def _parse_inquiry_rows(html: str) -> list[dict]:
    """상담내역 HTML에서 줄마다 문의번호·주문번호·상품번호·제목·문의일·상태 (최신순)."""
    rows: list[dict] = []
    for m in INQUIRY_ROW_PATTERN.finditer(html):
        chunk = m.group(1)
        link = INQUIRY_ROW_LINK.search(chunk)
        cells = INQUIRY_ROW_CELL.findall(chunk)
        if not link or len(cells) < 5:
            continue
        written = re.search(r"\d{4}\.\d{2}\.\d{2}", cells[2])
        if not written:
            continue
        rows.append({
            "inquiry_id": link.group(1),
            "ord_no": link.group(2).replace("-", ""),
            "goods_no": link.group(3),
            "text": html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", link.group(4)))).strip(),
            "written_on": datetime.strptime(written.group(0), "%Y.%m.%d").date(),
            "state": _cell_state(cells[4]),
        })
    return rows


def _cell_state(cell: str) -> str:
    """진행상태 칸의 <strong> 글자만 (접수 줄에는 [문의취소] 버튼 글자가 같이 있다)."""
    strong = re.search(r"<strong[^>]*>(.*?)</strong>", cell, re.S)
    text = strong.group(1) if strong else re.sub(r"<button.*?</button>", " ", cell, flags=re.S)
    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))).strip()


def _fetch_inquiry_page(context: BrowserContext, page_no: int) -> tuple[list[dict], int]:
    """상담내역 한 페이지 (줄들, 마지막 페이지 번호). 목록을 못 읽으면 ParseError - 모르는 채로 등록하지 않는다."""
    html = _get_html(context, INQUIRY_LIST_URL.format(page=page_no))
    if html is None or INQUIRY_LIST_MARKER not in html:
        raise ParseError("상담내역을 읽지 못했습니다 (로그인 세션이 없거나 화면이 바뀜).")
    pages = [int(n) for n in INQUIRY_PAGE_LINK.findall(html)] + [int(n) for n in INQUIRY_PAGE_ON.findall(html)]
    return _parse_inquiry_rows(html), max(pages or [page_no])


def _load_inquiry_rows(context: BrowserContext, since: date | None, *, refresh: bool = False,
                       max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> list[dict]:
    """since 이후 줄이 다 들어올 때까지 상담내역을 읽어둔 캐시(최신순). refresh면 1페이지부터 새로."""
    cache = _inquiry_rows_cache.get(id(context))
    if cache is None or refresh:
        rows, last = _fetch_inquiry_page(context, 1)
        cache = {"rows": rows, "pages": 1, "last": last}
        _inquiry_rows_cache[id(context)] = cache
    while (since is not None and cache["rows"] and cache["pages"] < min(max_pages, cache["last"])
           and cache["rows"][-1]["written_on"] >= since):
        more, _ = _fetch_inquiry_page(context, cache["pages"] + 1)
        if not more:
            break
        cache["rows"].extend(more)
        cache["pages"] += 1
    return cache["rows"]


def _describe_listed(entry: dict) -> str:
    return (f"{entry.get('state') or '상태 모름'} {entry['written_on']:%Y.%m.%d} "
            f"(문의번호 {entry['inquiry_id']}, 제목 '{entry['text']}')")


def _find_listed_inquiry(context: BrowserContext, ord_no: str, since: date | None, *,
                         refresh: bool = False, max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> dict | None:
    """상담내역에서 since 이후에 쓴, 이 주문번호의 '배송 언제' 문의를 찾는다 (사람이 직접 남긴 것 포함)."""
    for entry in _load_inquiry_rows(context, since, refresh=refresh, max_pages=max_pages):
        if since is not None and entry["written_on"] < since:
            return None   # 최신순이라 여기부터는 전부 더 오래된 것
        if entry["ord_no"] == ord_no and INQUIRY_SAME_MARK in entry["text"]:
            return entry
    return None


def _confirm_inquiry_listed(context: BrowserContext, ord_no: str, message: str) -> str:
    """등록 뒤 상담내역을 새로 받아 오늘 자로 올라갔는지 본다. 목록이 늦게 갱신될 수 있어 몇 번 다시 본다."""
    today = date.today()
    for attempt in range(1, INQUIRY_HISTORY_TRIES + 1):
        found = _find_listed_inquiry(context, ord_no, today, refresh=True, max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < INQUIRY_HISTORY_TRIES:
            common.sleep(INQUIRY_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"[등록]은 눌렀지만 상담내역에서 확인되지 않았습니다. "
        f"다시 남기기 전에 롯데아이몰 마이롯데 > 상담내역에서 '{message}'가 있는지 직접 확인해주세요.")


def _select_inquiry_types(form, ord_no: str) -> None:
    """문의 유형 분류 [배송/회수] > [배송문의]를 사람처럼 change로 고른다 (등록은 하지 않는다)."""
    if form.locator(INQUIRY_SUBMIT).count() == 0:
        raise ParseError(f"1:1 문의 화면이 뜨지 않았습니다 (주문번호={ord_no}, url={form.url}).")
    if form.locator("#ord_no").input_value().replace("-", "") != ord_no:
        raise ParseError(f"1:1 문의 화면에 이 주문이 연결되지 않았습니다 (주문번호={ord_no}).")
    options = [o.strip() for o in form.locator(f"{INQUIRY_TYPE_SELECT} option").all_inner_texts()]
    if INQUIRY_TYPE_LABEL not in options:
        raise ParseError(f"문의 유형 분류에 [{INQUIRY_TYPE_LABEL}]이 없습니다 (화면: {options}).")
    with form.expect_response(lambda r: INQUIRY_SUBTYPE_API in r.url, timeout=INQUIRY_STEP_WAIT_MS):
        form.select_option(INQUIRY_TYPE_SELECT, INQUIRY_TYPE_CODE)
    subtype = form.locator(f"{INQUIRY_SUBTYPE_SELECT} option[value='{INQUIRY_SUBTYPE_CODE}']")
    try:
        subtype.wait_for(state="attached", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        seen = form.locator(f"{INQUIRY_SUBTYPE_SELECT} option").all_inner_texts()
        raise ParseError(
            f"[{INQUIRY_TYPE_LABEL}]을 골랐는데 소분류에 [{INQUIRY_SUBTYPE_LABEL}]이 없습니다 (화면: {seen}).") from None
    label = subtype.inner_text().strip()
    if label != INQUIRY_SUBTYPE_LABEL:
        raise ParseError(f"소분류 코드 {INQUIRY_SUBTYPE_CODE}의 이름이 '{label}'입니다 - [{INQUIRY_SUBTYPE_LABEL}]이 아닙니다.")
    form.select_option(INQUIRY_SUBTYPE_SELECT, INQUIRY_SUBTYPE_CODE)
    chosen = (form.locator(INQUIRY_TYPE_SELECT).input_value(), form.locator(INQUIRY_SUBTYPE_SELECT).input_value())
    if chosen != (INQUIRY_TYPE_CODE, INQUIRY_SUBTYPE_CODE):
        raise ParseError(f"문의 유형이 {INQUIRY_TYPE_LABEL}/{INQUIRY_SUBTYPE_LABEL}로 잡히지 않았습니다 (화면: {chosen}).")


def _fill_inquiry_form(form, order: dict, message: str) -> None:
    """문의 화면에서 유형·제목·내용을 채운다 - [등록]은 누르지 않는다."""
    _select_inquiry_types(form, order["ord_no"])
    form.fill(INQUIRY_TITLE, message[:INQUIRY_TITLE_MAX])
    form.fill(INQUIRY_CONTENT, message[:INQUIRY_CONTENT_MAX])
    if form.locator(INQUIRY_TITLE).input_value().strip() != message:
        raise ParseError("문의 제목이 입력되지 않았습니다.")
    if form.locator(INQUIRY_CONTENT).input_value().strip() != message:
        raise ParseError("문의 내용이 입력되지 않았습니다.")
    if form.locator("#goods_no").input_value() != order["goods_no"]:
        raise ParseError(f"문의할 상품이 주문상세의 상품(goods_no={order['goods_no']})과 다릅니다.")


def _submit_via_form(context: BrowserContext, order: dict, message: str, headless: bool) -> str:
    """문의 화면을 열어 유형·제목·내용을 채우고 [등록]을 눌러 등록 POST가 나간 것까지 본다."""
    ord_no = order["ord_no"]
    page = context.new_page()
    page.route("**/*", _abort_third_party)
    dialogs: list[tuple[str, str]] = []
    posted: list[str] = []
    navigated: list[str] = []
    page.on("dialog", lambda d: (dialogs.append((d.type, d.message)), d.accept()))
    page.on("request", lambda r: posted.append(r.url) if INQUIRY_POST_PATH in r.url and r.method == "POST" else None)
    page.on("framenavigated", lambda f: navigated.append(f.url) if f == page.main_frame and posted else None)
    try:
        _goto_logged_in(context, page, INQUIRY_FORM_URL.format(**order), headless)
        if INQUIRY_FORM_MARKER not in page.url:
            raise ParseError(f"1:1 문의 화면 대신 다른 화면이 열렸습니다 (주문번호={ord_no}, url={page.url}).")
        _fill_inquiry_form(page, order, message)

        page.locator(INQUIRY_SUBMIT).first.click()
        # 사이트 JS: 상품번호 확인 -> 중복 문의 확인 -> 폼 POST -> 서버가 다시
        # 문의 폼 화면을 준다(2026-09-08 첫 정식 실등록 실측 - 주소가 같은
        # getinquireForm이라 주소 변화로는 이동을 알 수 없다). 그래서 등록 POST가
        # 나간 뒤 첫 화면 이동(framenavigated)까지, 또는 '중복 문의 알림'이나
        # 안내 alert이 오면 멈춘다 - 어느 것도 안 오면 시한 뒤 사유를 가려 올린다.
        deadline = time.monotonic() + INQUIRY_STEP_WAIT_MS / 1000
        while time.monotonic() < deadline:
            if navigated or dialogs or page.locator(INQUIRY_DUP_POPUP).count() > 0:
                break
            page.wait_for_timeout(100)
        if not posted:
            if page.locator(INQUIRY_DUP_POPUP).count() > 0:
                raise AlreadyInquired(
                    "롯데아이몰이 '중복 문의 알림'을 띄웠습니다 - 같은 주문·상품·유형의 문의를 상담사가 확인 중입니다.")
            seen = " / ".join(f"{t}: {m}" for t, m in dialogs) or "(뜬 창 없음)"
            raise ParseError(f"[등록]을 눌렀는데 등록되지 않았습니다 ({seen}).")
        with contextlib.suppress(PlaywrightTimeoutError):
            page.wait_for_load_state("domcontentloaded", timeout=INQUIRY_STEP_WAIT_MS)
        notes = " / ".join(m for _, m in dialogs)
        landed = urlparse(page.url).path
        return f"등록 요청 보냄 (화면 {landed}{' · ' + notes if notes else ''})"
    finally:
        page.close()


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """1:1 문의(배송/회수 > 배송문의)를 남기고 확인 문구를 돌려준다.

    취소/품절 주문은 남기지 않고, 상담내역에 이 주문의 같은 문의가 주문일 이후에
    이미 있으면 AlreadyInquired로 넘긴다. 등록 뒤 상담내역에 오늘 자로 올라갔는지
    확인한다. 어디서든 어긋나면 ParseError/BlockedError - 남겼는지 불확실한 채로
    성공이라 하지 않는다. 제목과 내용이 같은 문구다 (사용자 지시).
    """
    ord_no = extract_order_no(product_url)
    message = f"{recipient_name.strip()} 배송 언제 시작하나요?"
    order = _order_for_inquiry(context, product_url, ord_no, headless)
    existing = _find_listed_inquiry(context, ord_no, _order_date_of(ord_no))
    if existing is not None:
        raise AlreadyInquired(f"상담내역에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

    done = _submit_via_form(context, order, message, headless)
    listed = _confirm_inquiry_listed(context, ord_no, message)
    return f"{done} · 상담내역 확인: {listed}"
