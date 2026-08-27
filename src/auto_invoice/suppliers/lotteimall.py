"""롯데아이몰(LOTTE iMall / 롯데홈쇼핑) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.lotteimall.com/mypage/getOrderDtlInfo.lotte?ord_no=<주문번호>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고 ord_no만
  있으면 되도록 만들었다 - 실제로 다른 쿼리스트링 없이 ord_no 하나만 붙여도
  정상적으로 주문상세 페이지가 뜨는 것을 확인했다.)
- 로그인이 안 되어 있으면 https://www.lotteimall.com/member/login/forward.LCLoginMem.lotte
  로 리다이렉트된다. 로그인 폼 셀렉터: 아이디 "#login_id", 비밀번호 "#password".
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
from .base import BlockedError, ParseError, TrackingNotAvailableYet, normalize_option

load_dotenv()

LOGIN_ID_SELECTOR = "#login_id"

DOMAINS = {"lotteimall.com", "www.lotteimall.com"}
SITE_KEY = "lotteimall"

DEFAULT_COURIER = "택배"  # 팝업에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

TRACKING_LINK_TEXT = "배송추적"
TRACKING_PATTERN = re.compile(r"송장\s*번호\s+([0-9][0-9\-]{5,})")
COURIER_PATTERN = re.compile(r"택배사\s+([^\n(]+)")
NOT_YET_PATTERNS = ["주문접수", "결제완료", "상품준비중"]

# CJ대한통운/롯데택배가 화면에 축약형/음차 표기("씨제이대한통운", "CJ", "롯데")로 나올 수
# 있어 업로드 파일에는 정식 명칭으로 맞춰 넣는다 (다른 어댑터와 동일한 규칙).
COURIER_NORMALIZATION = [
    ("대한통운", "CJ대한통운"),
    ("CJ", "CJ대한통운"),
    ("롯데", "롯데택배"),
    ("DELIBOX", "딜리박스"),
]


def _normalize_courier(raw: str) -> str:
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword in raw:
            return canonical
    return raw


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ord_no")
    if not values:
        raise ParseError(f"URL에서 ord_no 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    if "login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    lotteimall_id = os.environ.get("LOTTEIMALL_ID")
    if not lotteimall_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(lotteimall_id)
    except Exception:
        pass


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


def _wait_for_manual_login(page) -> bool:
    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함
    return False


def _scrape_popup(popup) -> tuple[str, str]:
    popup.wait_for_load_state("domcontentloaded")
    popup.wait_for_timeout(1000)
    body_text = popup.inner_text("body")

    tracking_match = TRACKING_PATTERN.search(body_text)
    if not tracking_match:
        raise ParseError("배송추적 팝업에서 송장번호를 찾지 못했습니다.")
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    courier_match = COURIER_PATTERN.search(body_text)
    courier = _normalize_courier(courier_match.group(1).strip()) if courier_match else DEFAULT_COURIER

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
            if headless:
                raise BlockedError(
                    "롯데아이몰 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[lotteimall] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[lotteimall] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        return _scrape_tracking_from_page(context, page, order_no, order_option)
    finally:
        page.close()
