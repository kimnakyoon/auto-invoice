"""GS SHOP(GSSHOP) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세(배송현황) 팝업 URL: https://<호스트>/ord/dlvcursta/popup/ordDtl.gs?ordNo=<주문번호>&ecOrdTypCd=<S 등>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 팝업 URL이 그대로 들어있는 것으로 확인했다.)
  호스트는 with.gsshop.com과 www.gsshop.com 두 가지로 들어오는데, 경로/응답
  구조가 완전히 같고 로그인 쿠키도 .gsshop.com 스코프라 서로 공유된다 -
  들어온 URL의 호스트를 그대로 따라가면 된다.
- 로그인이 안 되어 있으면 /cust/login/login.gs?returnurl=... 로 리다이렉트되는데,
  이 returnurl은 원래 요청한 팝업 URL이 아니라 항상 홈(index.gs)으로 고정되어
  있었다(롯데온/지마켓과 다른 점) - 그래서 로그인 완료 후에도 자동으로 원래
  페이지로 돌아오지 않으므로, 다른 어댑터와 동일하게 로그인 감지 후 항상
  product_url로 명시적으로 다시 이동한다. 로그인 폼 셀렉터: 아이디 "#id",
  비밀번호 "#passwd".
- 로그인된 상태로 이 팝업 페이지를 열면 <script type="application/json"
  id="entry-data"> 안에 주문 전체가 JSON으로 그대로 들어있다(화면 렌더링과
  별개로 이미 응답에 포함되어 있음 - API 호출이나 버튼 클릭이 전혀 필요
  없다). ordItemList[] 각 항목의 invNo(송장번호)/dlvsCoCd(택배사 코드)/
  ordItemStExposNm(진행상태 텍스트)/exposAttrPrdNm(옵션)/exposPrdNm(상품명)/
  hopeDlvYn("E"면 새벽배송이라 아직 조회 불가)를 그대로 쓴다.
- dlvsCoCd는 "HD" 같은 내부 코드라 사람이 읽을 수 있는 택배사명이 아니다.
  화면의 "배송현황조회" 링크(data-action="dlvTrace")를 실제로 클릭하면
  /ord/dlvcursta/popup/dlvTrace.gs?ordNo=<주문번호>&ordItemId=<상품ID> 팝업이
  새 창으로 뜨는데, 이 페이지에 "택배업체  <정식명칭> 대표번호 : ..." 형태로
  실제 택배사명이 렌더링되어 있다(dlvsCoCd="HD" 확인 사례: 롯데택배). 이
  URL은 코드만 알면 그대로 다시 열 수 있어(팝업 클릭을 흉내낼 필요 없이)
  직접 이동해서 택배사명만 이 페이지에서 읽어온다 - 송장번호는 이미
  entry-data에서 얻은 값을 그대로 쓴다(더 신뢰할 수 있는 구조화된 값).
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import BlockedError, ParseError, TrackingNotAvailableYet, normalize_option

load_dotenv()

LOGIN_ID_SELECTOR = "#id"

# 같은 주문상세 팝업이 with.gsshop.com / www.gsshop.com 두 호스트로 모두
# 들어온다(경로와 응답 구조는 동일). registry는 "www." 접두사를 떼고 찾지만,
# 다른 어댑터와 표기를 맞추려고 www 형태도 같이 적어둔다.
DOMAINS = {"with.gsshop.com", "gsshop.com", "www.gsshop.com"}
SITE_KEY = "gsshop"

TRACE_PATH = "/ord/dlvcursta/popup/dlvTrace.gs?ordNo={ord_no}&ordItemId={ord_item_id}"

DEFAULT_COURIER = "택배"  # 배송현황조회 팝업에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

# "택배업체\t롯데택배 대표번호 : 1588-2121" 형태 - 대표번호가 없는 택배사도
# 있을 수 있어(예: 편의점 락커 배송) "대표번호"가 없으면 줄 끝까지를 쓴다.
COURIER_PATTERN = re.compile(r"택배업체[\t ]*([^\n]+?)(?:\s+대표번호|$)", re.MULTILINE)
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문확인중", "입금대기"]

# CJ대한통운/롯데택배가 화면에 축약형("CJ", "대한통운", "롯데")으로 나올 수
# 있어 업로드 파일에는 정식 명칭으로 맞춰 넣는다 (다른 어댑터와 동일한 규칙).
# DELIBOX(무인택배함/딜리박스)는 코드가 그대로 노출되는 경우가 있어 별도 매핑.
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
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def extract_origin(product_url: str) -> str:
    """배송현황조회 팝업을 주문상세와 같은 호스트에서 연다.

    with.gsshop.com 주문을 www.gsshop.com으로(또는 그 반대로) 열면 불필요한
    호스트 이동이 생기므로, 들어온 상품URL의 호스트를 그대로 따라간다.
    """
    parsed = urlparse(product_url)
    if not parsed.scheme or not parsed.netloc:
        raise ParseError(f"상품URL 형식을 해석할 수 없습니다: {product_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    if "/cust/login/login.gs" not in page.url:
        return False
    return page.locator("input[type='password']").count() > 0


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    gsshop_id = os.environ.get("GSSHOP_ID")
    if not gsshop_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(gsshop_id)
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


def _read_entry_data(page, ord_no: str) -> dict:
    locator = page.locator("#entry-data")
    if locator.count() == 0:
        raise ParseError(f"주문 정보(entry-data)를 찾지 못했습니다 (주문번호={ord_no}).")
    try:
        return json.loads(locator.inner_text())
    except Exception as e:
        raise ParseError(f"주문 정보(entry-data) 파싱에 실패했습니다 (주문번호={ord_no}): {e}") from e


def _find_item_by_order_option(shipped: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션" 값으로 상품을 정확히 짚을 수 있으면 그걸
    쓴다. 매칭이 0개(표기가 서로 안 맞음)거나 2개 이상(애매함)이면 None을
    반환해서 호출자가 기존 방식(개수 비교)으로 넘어가게 한다."""
    if len(shipped) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    matched = [
        item
        for item in shipped
        if target in normalize_option(item.get("exposAttrPrdNm")) or target in normalize_option(item.get("exposPrdNm"))
    ]
    return matched[0] if len(matched) == 1 else None


def _select_item(entry: dict, ord_no: str, order_option: str | None) -> dict:
    items = entry.get("ordItemList") or []
    if not items:
        raise ParseError(f"주문 응답에 상품 정보가 없습니다 (주문번호={ord_no}).")

    # 새벽배송(hopeDlvYn="E")은 아직 조회 시점이 아니면 송장이 있어도 화면에서
    # 막아둔다(사이트 자체가 "배송현황조회 가능 시간이 아닙니다"라고 안내함) -
    # invNo가 비어있는 경우와 동일하게 미발급으로 취급한다.
    shipped = [it for it in items if it.get("invNo") and it.get("hopeDlvYn") != "E"]
    if not shipped:
        status_text = items[0].get("ordItemStExposNm", "알 수 없음")
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={ord_no}, 상태={status_text}).")

    matched = _find_item_by_order_option(shipped, order_option)
    if matched is not None:
        return matched

    tracking_nos = {it["invNo"] for it in shipped}
    if len(tracking_nos) > 1 and len(items) != len(tracking_nos):
        # 상품 개수와 송장 개수가 다르면 아직 일부만 발송된 것인지, 일부 상품이
        # 같은 박스로 묶여 나간 것인지 텍스트만으로는 구분할 수 없다 - 안전하게
        # 사람이 확인하게 한다 (무신사 어댑터와 동일한 규칙).
        raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={ord_no}) - 상품별로 나눠 배송된 것으로 보입니다.")

    return shipped[0]


def _fetch_courier_name(page, origin: str, ord_no: str, ord_item_id: str) -> str:
    trace_url = origin + TRACE_PATH.format(ord_no=ord_no, ord_item_id=ord_item_id)
    page.goto(trace_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    body_text = page.inner_text("body")
    match = COURIER_PATTERN.search(body_text)
    if not match:
        return DEFAULT_COURIER
    return _normalize_courier(match.group(1).strip())


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    ord_no = extract_order_no(product_url)
    origin = extract_origin(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if headless:
                raise BlockedError(
                    "GSSHOP 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[gsshop] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[gsshop] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            # GSSHOP은 로그인 후 원래 페이지가 아니라 항상 홈으로 이동하므로
            # 명시적으로 다시 이동해야 한다.
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        entry = _read_entry_data(page, ord_no)
        item = _select_item(entry, ord_no, order_option)
        tracking_no = re.sub(r"[^0-9]", "", str(item["invNo"]))
        courier = _fetch_courier_name(page, origin, ord_no, str(item["ordItemId"]))

        return TrackingResult(tracking_no=tracking_no, courier=courier)
    finally:
        page.close()
