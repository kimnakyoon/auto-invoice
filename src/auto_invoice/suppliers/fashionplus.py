"""패션플러스(FASHION PLUS) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.fashionplus.co.kr/mypage/order/detail/<주문번호>
- 로그인 폼은 id="login_id" 입력창(Vue 앱)을 쓴다. 로그인이 안 되어 있으면
  보호된 페이지 접근 시 /auth/login 으로 리다이렉트된다.
- 각 상품 옆 "배송조회" 링크는 새 창으로 열리는 <a href> 링크이고, href가
  바로 https://trace.goodsflow.com/VIEW/V1/whereis/fashionplus/<주문번호>-<상품순번>
  형태다. goodsflow(배송지키미)는 패션플러스와 별개인 3자 배송조회 서비스로,
  로그인/세션 쿠키가 전혀 필요 없는 공개 조회 페이지다 - 그래서 새 탭을 열어
  화면 텍스트를 스크래핑하는 대신(택배사명이 <img alt="한진택배"> 형태라 화면
  텍스트만으로는 못 읽는다), 그 페이지가 내부적으로 호출하는 JSON API를
  context.request로 직접 호출한다:
    POST https://trace.goodsflow.com/VIEW/api/tracking
    body: {"memberCode": "fashionplus", "uniqueCode": "<주문번호>-<상품순번>"}
  응답의 baseData.logisticsName / baseData.invoiceNo 에 택배사/송장번호가
  그대로 들어있다. 이 사이트가 CORS를 프론트엔드(goodsflow 자체 도메인)에서만
  허용해서 패션플러스 페이지에서 직접 fetch하면 막히지만, Playwright의
  context.request(브라우저 fetch가 아니라 별도 HTTP 클라이언트)는 CORS
  제약이 없어 문제없이 호출된다(무신사 어댑터와 동일한 이유).
- 상품순번(-1, -2, ...)은 화면에 상품이 나열된 순서와 같다. 주문상세
  페이지에서 "배송조회" 링크를 DOM 순서대로 모두 수집해서, 몇 번째 링크인지로
  샵마인 "주문옵션" 값과 매칭한다(롯데온/네이버 어댑터와 동일한 패턴).
  실제로 확인한 사례로, 한 주문에 상품이 2개라도 같은 박스로 묶여 나가면
  두 상품 다 같은 송장번호를 돌려주기도 한다 - 이 경우는 서로 다른 척 하지
  않고 그냥 그 값을 대표로 쓴다(다른 어댑터의 "송장번호 개수 비교" 규칙과 동일).
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import BlockedError, ParseError, TrackingNotAvailableYet, normalize_option, raise_if_cancelled

load_dotenv()

LOGIN_ID_SELECTOR = "#login_id"

DOMAINS = {"fashionplus.co.kr", "www.fashionplus.co.kr"}
SITE_KEY = "fashionplus"

ORDER_DETAIL_URL = "https://www.fashionplus.co.kr/mypage/order/detail/{order_no}"

GOODSFLOW_API_URL = "https://trace.goodsflow.com/VIEW/api/tracking"
GOODSFLOW_MEMBER_CODE = "fashionplus"

DEFAULT_COURIER = "택배"  # goodsflow 응답에 택배사명이 비어있을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

TRACKING_LINK_TEXT = "배송조회"
NOT_YET_PATTERNS = ["배송준비중", "결제완료", "입금대기", "주문확인중"]

# CJ대한통운/롯데택배가 화면/응답에 축약형("CJ", "대한통운", "롯데")으로 나올 수
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
    match = re.search(r"/order/detail/(\d+)", product_url)
    if match:
        return match.group(1)
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[-1].isdigit():
        return segments[-1]
    raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    return "/auth/login" in page.url


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    fashionplus_id = os.environ.get("FASHIONPLUS_ID")
    if not fashionplus_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(fashionplus_id)
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


def _collect_tracking_links(page) -> list[str]:
    """"배송조회" 링크의 href를 화면에 나온 순서 그대로 수집한다.

    화면에서 "배송조회" 텍스트는 <a> 안의 <span>에 들어있어(get_by_text로
    찾으면 href가 없는 그 span이 잡힌다), 반드시 <a> 태그 자체를 찾아야 한다.
    """
    locator = page.locator(f"a:has-text('{TRACKING_LINK_TEXT}')")
    count = locator.count()
    hrefs = []
    for i in range(count):
        href = locator.nth(i).get_attribute("href")
        if href:
            hrefs.append(href)
    return hrefs


def _unique_code_from_href(href: str) -> str | None:
    match = re.search(r"/whereis/[^/]+/([^/?#]+)", href)
    return match.group(1) if match else None


def _fetch_goodsflow_tracking(context: BrowserContext, unique_code: str) -> dict | None:
    resp = context.request.post(
        GOODSFLOW_API_URL,
        data=json.dumps({"memberCode": GOODSFLOW_MEMBER_CODE, "uniqueCode": unique_code}),
        headers={"Content-Type": "application/json"},
    )
    if resp.status != 200:
        return None
    data = resp.json()
    if not data.get("isSuccess"):
        return None
    return data.get("baseData")


def _select_by_order_option(body_text: str, link_count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 몇 번째 "배송조회" 링크 앞 텍스트에만
    유일하게 나타나면 그 인덱스를 쓴다. 0개(표기가 안 맞음) 또는 2개 이상
    (애매함) 매칭되면 None - 호출자가 기존 방식(개수 비교)으로 넘어간다."""
    if link_count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None

    positions = [m.start() for m in re.finditer(re.escape(TRACKING_LINK_TEXT), body_text)]
    if len(positions) != link_count:
        return None  # 텍스트와 링크 개수가 안 맞으면(예상치 못한 구조) 안전하게 포기

    candidates = []
    prev_end = 0
    for idx, pos in enumerate(positions):
        window = body_text[max(prev_end, pos - 400) : pos]
        if target in normalize_option(window):
            candidates.append(idx)
        prev_end = pos
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(
    context: BrowserContext, page, order_no: str, order_option: str | None = None
) -> TrackingResult:
    hrefs = _collect_tracking_links(page)
    if not hrefs:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    tracked: list[tuple[str, dict]] = []
    for href in hrefs:
        unique_code = _unique_code_from_href(href)
        if unique_code is None:
            continue
        base_data = _fetch_goodsflow_tracking(context, unique_code)
        if base_data and base_data.get("invoiceNo"):
            tracked.append((unique_code, base_data))

    if not tracked:
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")

    distinct_tracking_nos = {t[1]["invoiceNo"] for t in tracked}
    chosen: dict | None = None
    if len(distinct_tracking_nos) > 1:
        body_text = page.inner_text("body")
        matched_idx = _select_by_order_option(body_text, len(hrefs), order_option)
        if matched_idx is not None and matched_idx < len(tracked):
            chosen = tracked[matched_idx][1]
        else:
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
    else:
        chosen = tracked[0][1]

    tracking_no = re.sub(r"[^0-9]", "", chosen["invoiceNo"])
    courier = _normalize_courier((chosen.get("logisticsName") or "").strip() or DEFAULT_COURIER)
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    url = ORDER_DETAIL_URL.format(order_no=order_no)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if headless:
                raise BlockedError(
                    "패션플러스 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[fashionplus] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[fashionplus] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        return _scrape_tracking_from_page(context, page, order_no, order_option)
    finally:
        page.close()
