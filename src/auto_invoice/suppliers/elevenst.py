"""11번가(buy.11st.co.kr) 공급사 어댑터.

리버스엔지니어링 결과:
- 샵마인 엑셀의 "상품URL" 컬럼에 실제로 들어있는 형태(내보내기 파일에서 확인):
  https://buy.11st.co.kr/order/BuyManager.tmall?method=getOrderDetailInfo&ordNo=<주문번호>&isSSL=Y
  화면상으로는 주문목록에서 "상세보기"가 POST 폼 전송(BuyManager.tmall)으로
  열리지만, 위 URL은 GET으로도 그대로 열리는 것을 확인했다. 그래서 다른
  어댑터와 똑같이 product_url을 goto하기만 하면 된다.
- 로그인이 안 되어 있으면 login.11st.co.kr/auth/v2/login?...&returnURL=... 으로
  리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#memId, 비밀번호 input#memPwd,
  로그인 버튼 button#loginButton. 사용자가 "첫 로그인부터 쿠키로 자동
  로그인"을 요청했고, 실제로 아이디+비밀번호를 채우고 로그인 버튼을 자동
  클릭해도 캡차 등에 막히지 않는 것을 확인했다 (SSG/더현대/NS홈쇼핑과 동일한
  패턴). 그래서 ELEVENST_ID/ELEVENST_PW 환경변수로 완전 자동 로그인하고,
  로그인 세션은 storage_state(auth/elevenst_state.json)에 저장되어 이후
  실행부터는 쿠키만으로 바로 조회된다.
  ("로그인 상태 유지" 체크박스(#lbAutoLogin)도 있지만, 체크하면 공용PC 주의
  안내 모달(#arModalLoginNudge)이 떠서 로그인 버튼 클릭 자체를 가로막는 것을
  확인했다. storage_state로 쿠키가 이미 보존되므로 체크하지 않는다.)
- 주문상세 페이지에는 송장번호가 없다. 상품(주문상태 칸)마다
  <a href="javascript:goDeliveryTracking('<dlvNo>');">배송조회</a> 링크만 있고,
  이 dlvNo는 11번가 내부 배송번호이지 송장번호가 아니다 (예: dlvNo 2721476603 ->
  실제 송장번호 304318936344). 실제 송장번호/택배사는 그 링크가 여는
  https://buy.11st.co.kr/delivery/trace.tmall?dlvNo=<dlvNo> 페이지의
  div.delivery_info 안 "택배사"/"송장번호" 필드에 있다. 이 페이지에는 주문번호도
  같이 나와서, 엉뚱한 주문의 송장을 가져오지 않았는지 검증할 수 있다.
- 미발송 판단: 주문상세 페이지 하단에는 "배송진행순서 ... 3.배송준비중 ..."
  같은 고정 안내문이 항상 붙어 있어서, 본문 전체에서 "배송준비중"을 찾으면
  발송된 주문까지 미발송으로 오판한다. 그래서 안내문 시작("알아두세요!") 앞
  구간만 잘라서 상태 문구를 본다.
- 상품이 여러 개면 "배송조회" 링크도 여러 개 뜬다. 11번가 주문상세는 상품
  한 개가 <tr> 하나라서, 링크가 속한 <tr>의 텍스트에 샵마인 엑셀의 "주문옵션"
  값이 들어있는지로 어느 상품인지 정확히 특정할 수 있다. 특정할 수 없으면
  (다른 어댑터와 동일한 안전 규칙) 전부 조회해서 실제로 서로 다른 송장인지
  비교하고, 다르면 사람이 확인하도록 예외를 던진다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

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

LOGIN_ID_SELECTOR = "#memId"
LOGIN_PW_SELECTOR = "#memPwd"
LOGIN_BUTTON_SELECTOR = "#loginButton"

# 로그인 버튼을 누른 직후 뜨는 모달들. 이걸 처리하지 않으면 로그인은 됐는데도
# 화면이 로그인 페이지에 머물러 있어 "로그인 실패"로 오판한다.
#   - "PC 로그인 상태 유지 안내": "네, 좋아요"를 눌러야 쿠키가 오래 유지되므로
#     (사용자가 원한 "쿠키로 자동 로그인"에 유리) 등록 버튼을 누른다.
#   - "간편 로그인(패스키) 등록 안내": 굳이 등록할 필요가 없어 닫기만 한다.
POST_LOGIN_MODAL_BUTTONS = [
    "button[modal-auto-action='register']",  # 로그인 상태 유지 - 네, 좋아요
    "#btnSpLoginClose",  # 간편 로그인 등록 안내 - 닫기
]

DOMAINS = {"buy.11st.co.kr", "11st.co.kr", "www.11st.co.kr", "m.11st.co.kr"}
SITE_KEY = "elevenst"

LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초

TRACE_URL = "https://buy.11st.co.kr/delivery/trace.tmall?dlvNo={dlv_no}"
TRACKING_LINK_SELECTOR = "a[href*='goDeliveryTracking']"
DLV_NO_PATTERN = re.compile(r"goDeliveryTracking\('([^']+)'")

# 주문상세 본문에서 이 문구부터는 고정 안내문이라 상태 판단에서 제외한다.
GUIDE_ANCHOR = "알아두세요!"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "입금대기중", "주문확인중"]

# 배송추적 페이지의 "택배사"/"송장번호" 필드
DELIVERY_FIELD_SELECTOR = "div.delivery_info div.field"
COURIER_FIELD_LABEL = "택배사"
TRACKING_FIELD_LABEL = "송장번호"
# 배송추적 페이지 하단 "주문정보"의 항목명/값 (주문번호 검증용)
INFO_ITEM_SELECTOR = "p.prd_info"
ORDER_NO_LABEL = "주문번호"

# "CJ대한통운 1588-1255"처럼 택배사명 뒤에 고객센터 전화번호가 붙어서 나온다.
COURIER_PHONE_PATTERN = re.compile(r"\s*[0-9][0-9\-]{5,}\s*$")

DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(page, lambda url: "login.11st.co.kr" in url)


def _dismiss_post_login_modals(page: Page) -> None:
    """로그인 직후 뜨는 안내 모달을 닫는다 (뜨지 않았으면 아무것도 하지 않는다)."""
    for selector in POST_LOGIN_MODAL_BUTTONS:
        try:
            button = page.locator(selector)
            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=3000)
                page.wait_for_timeout(500)
        except Exception:
            # 모달이 이미 닫혔거나 페이지가 넘어가는 중일 수 있다 - 무시하고 계속 대기한다.
            continue


def _auto_login(page: Page) -> bool:
    """ELEVENST_ID/ELEVENST_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    SSG/더현대/NS홈쇼핑 어댑터와 동일한 패턴 - 11번가도 자동 클릭 로그인이
    캡차 등에 막히지 않는 것을 확인했다.
    """
    login_id = os.environ.get("ELEVENST_ID")
    login_pw = os.environ.get("ELEVENST_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "11번가 로그인이 필요하지만 ELEVENST_ID/ELEVENST_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    page.click(LOGIN_BUTTON_SELECTOR)

    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        _dismiss_post_login_modals(page)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500
    return False


def _order_area_text(page: Page) -> str:
    """페이지 하단 고정 안내문("배송진행순서" 등)을 잘라낸 본문.

    안내문에 "배송준비중" 같은 단어가 항상 들어있어서, 자르지 않으면 이미
    발송된 주문도 미발송으로 오판한다.
    """
    text = page.inner_text("body")
    idx = text.find(GUIDE_ANCHOR)
    return text[:idx] if idx != -1 else text


def _read_field_value(page: Page, label: str) -> str | None:
    """배송추적 페이지 div.delivery_info 안에서 dt가 label인 field의 dd 텍스트."""
    fields = page.locator(DELIVERY_FIELD_SELECTOR)
    for i in range(fields.count()):
        field = fields.nth(i)
        try:
            if field.locator("dt").inner_text().strip() == label:
                return field.locator("dd").inner_text().strip()
        except Exception:
            continue
    return None


def _read_trace_order_no(page: Page) -> str | None:
    """배송추적 페이지 하단 "주문정보"의 주문번호 (엉뚱한 주문인지 검증용)."""
    items = page.locator(INFO_ITEM_SELECTOR)
    for i in range(items.count()):
        item = items.nth(i)
        try:
            if item.locator("span.tit_prd").inner_text().strip() == ORDER_NO_LABEL:
                return item.locator("span.txt_prd").inner_text().strip()
        except Exception:
            continue
    return None


def _field_values(html: str) -> dict[str, str]:
    """배송추적 페이지 div.delivery_info 의 <dt>라벨</dt><dd>값</dd> 쌍."""
    return {label.strip(): _strip_tags(value)
            for label, value in re.findall(r"<dt>\s*([^<]+?)\s*</dt>\s*<dd>(.*?)</dd>", html, re.S)}


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _fetch_tracking_by_dlv_no(context: BrowserContext, dlv_no: str, order_no: str) -> tuple[str, str]:
    """배송추적 페이지를 HTML로 받아 (송장번호, 택배사)를 읽는다.

    이 페이지는 서버가 그려서 내려주므로(자바스크립트 필요 없음) 화면을 열지
    않고 context.request로 받는다 - 새 페이지를 열고 1.5초 자던 예전 방식
    (2.5초)이 0.3초로 준다 (2026-09-02 실측).
    """
    response = context.request.get(TRACE_URL.format(dlv_no=dlv_no))
    if "login.11st.co.kr" in response.url:
        raise BlockedError(f"배송추적 페이지에서 로그인이 풀렸습니다 (주문번호={order_no}).")
    html = response.text()

    info_items = [_strip_tags(x) for x in re.findall(r'<p class="prd_info"[^>]*>(.*?)</p>', html, re.S)]
    trace_order_no = next((item.replace(ORDER_NO_LABEL, "", 1).strip()
                           for item in info_items if item.startswith(ORDER_NO_LABEL)), None)
    if trace_order_no and trace_order_no != order_no:
        raise ParseError(
            f"배송추적 페이지의 주문번호({trace_order_no})가 조회하려던 주문번호({order_no})와 다릅니다."
        )

    fields = _field_values(html)
    raw_tracking = fields.get(TRACKING_FIELD_LABEL)
    if not raw_tracking:
        raise TrackingNotAvailableYet(
            f"배송추적 페이지에 아직 송장번호가 없습니다 (주문번호={order_no}, dlvNo={dlv_no})."
        )
    tracking_no = re.sub(r"[^0-9]", "", raw_tracking)
    if not tracking_no:
        raise ParseError(f"송장번호를 숫자로 읽지 못했습니다: {raw_tracking!r} (주문번호={order_no}).")

    raw_courier = fields.get(COURIER_FIELD_LABEL) or ""
    # "CJ대한통운 1588-1255" -> "CJ대한통운"
    courier_name = COURIER_PHONE_PATTERN.sub("", raw_courier).strip()
    courier = common.normalize_courier(courier_name) if courier_name else DEFAULT_COURIER
    return tracking_no, courier


def _collect_dlv_nos(page: Page) -> list[tuple[str, str]]:
    """주문상세의 "배송조회" 링크마다 (dlvNo, 그 링크가 속한 <tr>의 텍스트)를 모은다."""
    links = page.locator(TRACKING_LINK_SELECTOR)
    collected: list[tuple[str, str]] = []
    for i in range(links.count()):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        match = DLV_NO_PATTERN.search(href)
        if not match:
            continue
        try:
            row_text = link.locator("xpath=ancestor::tr[1]").inner_text()
        except Exception:
            row_text = ""
        collected.append((match.group(1), row_text))
    return collected


def _select_by_order_option(candidates: list[tuple[str, str]], order_option: str | None) -> str | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 상품 행(<tr>) 텍스트에만 유일하게
    나타나면 그 행의 dlvNo를 쓴다. 0개(표기가 안 맞음) 또는 2개 이상(애매함)
    매칭되면 None - 호출자가 전부 조회해서 비교하는 방식으로 넘어간다."""
    if len(candidates) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    matched = [dlv_no for dlv_no, row_text in candidates if target in normalize_option(row_text)]
    return matched[0] if len(matched) == 1 else None


def _scrape_tracking_from_page(
    context: BrowserContext, page: Page, order_no: str, order_option: str | None
) -> TrackingResult:
    candidates = _collect_dlv_nos(page)

    if not candidates:
        area_text = _order_area_text(page)
        normalized_area = normalize_option(area_text)
        if any(normalize_option(p) in normalized_area for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(area_text, order_no)
        raise ParseError(f"화면에서 배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    if len(candidates) == 1:
        tracking_no, courier = _fetch_tracking_by_dlv_no(context, candidates[0][0], order_no)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    matched_dlv_no = _select_by_order_option(candidates, order_option)
    if matched_dlv_no is not None:
        tracking_no, courier = _fetch_tracking_by_dlv_no(context, matched_dlv_no, order_no)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 조회해서 실제로 서로 다른 송장인지 확인한다
    # (다른 어댑터와 동일한 안전 규칙).
    results = [_fetch_tracking_by_dlv_no(context, dlv_no, order_no) for dlv_no, _ in candidates]
    if len({tracking_no for tracking_no, _ in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다."
        )

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
            common.safe_print("[11st] 로그인 세션이 없어 자동 로그인을 시도합니다.")
            if not _auto_login(page):
                raise BlockedError("11번가 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("11번가 로그인 후에도 여전히 로그인 페이지입니다.")

        # 화면이 아직 덜 그려진 채로 읽으면 '아직 미발급'으로 잘못 넘길 수 있다
        # (조용히 틀리는 쪽이라 특히 위험하다). 그 주문의 주문번호가 화면에
        # 뜨면 다 그려진 것이다 - '배송조회' 같은 글자는 상단 메뉴에도 있어서
        # 표식으로 쓰면 덜 그려진 화면을 다 그려진 것으로 볼 수 있다.
        common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()
