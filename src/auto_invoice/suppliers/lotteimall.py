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
  - headless 브라우저로는 로그인 페이지 자체가 HTTP 403으로 막힌다(2026-08-28
    확인). 그래서 자동 로그인은 headless가 아닐 때만 시도한다.
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


def _auto_login(page) -> bool:
    """LOTTEIMALL_ID/LOTTEIMALL_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 경우를 위해 수동
    로그인 경로를 그대로 남겨뒀다).

    롯데온 어댑터와 같은 패턴이다 - 이 사이트도 로그인 실패를 화면 문구가
    아니라 alert()으로 알려주기 때문에 dialog 핸들러로 그 문구를 받아 실패
    사유째로 올린다. 핸들러가 없으면 Playwright가 alert을 조용히 닫아버려서
    원인도 모른 채 대기 시간만 다 쓰고 실패한다.
    """
    login_id = os.environ.get("LOTTEIMALL_ID")
    login_pw = os.environ.get("LOTTEIMALL_PW")
    if not login_id or not login_pw:
        return False

    if _captcha_visible(page):
        raise BlockedError(
            "롯데아이몰이 보안문자(캡차)를 요구하고 있어 자동 로그인을 할 수 없습니다 "
            "- 뜬 브라우저 창에서 직접 로그인해주세요."
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
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인 자체는
            # 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를 alert보다
            # 먼저 본다 (롯데온과 같은 이유).
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"롯데아이몰 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1500

        if _captcha_visible(page):
            raise BlockedError(
                "롯데아이몰이 로그인 도중 보안문자(캡차)를 요구했습니다 "
                "- 뜬 브라우저 창에서 직접 로그인해주세요."
            )
        raise BlockedError(
            "롯데아이몰 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
            "(추가 본인인증을 요구받았을 수 있습니다 - 브라우저 창을 확인해주세요)."
        )
    finally:
        page.remove_listener("dialog", _on_dialog)


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _scrape_popup(popup) -> tuple[str, str]:
    popup.wait_for_load_state("domcontentloaded")
    # 팝업에 송장번호가 뜰 때까지만 기다린다 - 예전에는 무조건 1초를 잤다.
    # 끝내 안 뜨면 예전과 같은 1초를 채우고 아래에서 ParseError로 넘어간다.
    body_text = common.wait_for_match(
        popup, lambda: popup.inner_text("body"), TRACKING_PATTERN, timeout_ms=1000)

    tracking_match = TRACKING_PATTERN.search(body_text)
    if not tracking_match:
        raise ParseError("배송추적 팝업에서 송장번호를 찾지 못했습니다.")
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    courier_match = COURIER_PATTERN.search(body_text)
    courier = common.normalize_courier(courier_match.group(1).strip()) if courier_match else DEFAULT_COURIER

    return tracking_no, courier


def _click_tracking_link(context: BrowserContext, link) -> tuple[str, str]:
    with context.expect_page(timeout=10000) as popup_info:
        link.click()
    popup = popup_info.value
    try:
        return _scrape_popup(popup)
    finally:
        popup.close()


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
            # headless로는 로그인 페이지 자체가 403으로 막혀서 자동 로그인도 못 한다.
            if headless:
                raise BlockedError(
                    "롯데아이몰 로그인이 필요합니다. 이 사이트는 headless 브라우저의 로그인 페이지 접근을 "
                    "막기 때문에, --headless 없이 실행해주세요 (.env에 LOTTEIMALL_PW가 있으면 자동 로그인합니다)."
                )
            if _auto_login(page):
                common.safe_print("[lotteimall] 로그인 세션이 없어 자동 로그인했습니다.")
            else:
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
