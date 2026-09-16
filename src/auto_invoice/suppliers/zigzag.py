"""지그재그(zigzag.kr) 공급사 어댑터.

리버스엔지니어링 결과 (2026-09-16 실측):
- 주문·배송 목록은 https://zigzag.kr/checkout/orders 이고, 실제 데이터는
  카카오스타일 GraphQL API에서 온다:
    POST https://api.zigzag.kr/api/2/graphql/GetUserSearchedOrderList
    변수: status_list(null), date_created_from/to(밀리초 epoch), last_id/last_date_created(커서)
  응답의 order_item 하나가 상품 한 줄이고, 아래 필드를 그대로 준다:
    order.order_number  주문번호(예: 139748128874868854)
    order.date_created  주문 시각(밀리초)
    order_item_number   상품별 주문 항목 번호
    status / item_status  진행상태(영문 enum). 실측: SHIPMENT_PROCESS_REQUESTED
                          (주문확인중), SHIPPED(배송완료), CONFIRMED(구매확정),
                          RETURNED(반품완료) 등
    shipping_company    택배사 **코드**(CJ / LOTTE / LOGEN / HANJIN / POSTAL / ...)
    invoice_number      송장번호(발송된 줄에만)
    product_info.options / order_item_product.option_detail_list  주문옵션
    fulfillment_info.date_expected_arrival_text  "내일(목) 이내 발송 예정" 같은 안내
  이 API는 **브라우저 화면 없이 context.request로** 쿠키만 있으면 받는다.
  로그인이 안 되어 있으면 200에 errors[0].extensions.code = "route_not_logged_in"
  ("로그인을 해주세요.")이 온다. 이 코드로 로그인 필요를 판정한다.
- 택배사 코드는 아래 COURIER_CODE_MAP으로 한글 이름으로 바꾼 뒤(CJ->CJ대한통운,
  LOTTE->롯데택배 등) common.normalize_courier를 한 번 더 거친다 - 사용자 요청
  4·5번(CJ/대한통운->CJ대한통운, DELIBOX->딜리박스, 롯데->롯데택배)을 그대로 지킨다.
  코드표에 없는 값은 normalize_courier(원문)로만 맞춘다.
- 로그인(ZIGZAG_ID/ZIGZAG_PW, 이메일/비밀번호)은 GraphQL LoginForWeb 뮤테이션인데,
  번들 크로미엄(headless)이나 request로 아이디/비밀번호를 바로 보내면 서버가
  "invalid session"(route 쿠키 없음) 또는 "잘못된 접근"(auth_invalid_access,
  봇 점수 미달)으로 거부한다. 그래서 옥션/W컨셉과 같은 근거로 **로그인만 우리가
  직접 실행해 CDP로 붙은 진짜 크롬 창**에서 한다(browser.real_chrome_cdp_context,
  navigator.webdriver=false). 실측(2026-09-16)으로 홈을 잠깐 열어 봇 지문
  쿠키(ZIGZAG_FINGERPRINT)를 만들고, 이메일 로그인 폼에서 사람처럼 한 글자씩
  입력한 뒤 Enter를 치면 첫 시도에 통과했다(약 15초). 성공하면 쿠키만 원래
  headless 조회 컨텍스트로 옮기고, orchestrator가 끝에 storage_state
  (auth/zigzag_state.json)로 저장해 다음 실행부터는 쿠키만으로 바로 조회한다
  (사용자 요청 3번 "쿠키로 첫 로그인부터 자동"). 로그인 세션(connect.sid)은
  자동로그인이라 만료가 길다(실측 만료 2027년).
- 샵마인 엑셀의 "상품URL"에 주문번호(숫자 18자리)가 들어 있으면 그 주문으로
  바로 좁히고, 없으면 옥션·갤러리아몰과 같은 방식으로 **주문옵션(+수령인)**으로
  어느 주문의 어느 줄인지 고른다(사용자 요청 6번 "찾기 힘들 때는 주문 옵션을
  비교"). 옵션이 같은 주문이 여러 건이면 수령인까지 봐서 확정한다 - 수령인은
    POST .../GetShippingGroupListForClaim  변수: order_number
    -> order.order_receiver.first_name  (가려지지 않은 전체 이름, 예 "김해용")
  으로 읽는다(orchestrator가 WANTS_RECIPIENT_NAME을 보고 recipient_name을 넘겨준다).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from .. import browser as browser_mod
from .. import eta as eta_mod
from ..models import TrackingResult
from . import common
from .auction import option_score, recipient_matches
from .base import (
    AdapterError,
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    ShipmentDelayed,
    attach_order_date,
    normalize_option,
)

load_dotenv()

DOMAINS = {"zigzag.kr", "www.zigzag.kr"}
SITE_KEY = "zigzag"

# 옥션·갤러리아몰과 같은 표시 - 상품URL만으로 주문을 못 고를 때 수령인까지 본다.
WANTS_RECIPIENT_NAME = True
# 화면 없이 가벼운 GraphQL 요청만 보내고 봇 확인도 없는 조회 경로 - 4910·W컨셉과 같은 간격.
REQUEST_GAP = (0.5, 1.2)

# 조회 API가 HeadlessChrome UA로도 답하지만, 로그인 쿠키를 만든 진짜 크롬과
# 같은 UA로 맞춰 둔다 (orchestrator가 CONTEXT_KWARGS를 조회 컨텍스트에 그대로 넘긴다).
NORMAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
CONTEXT_KWARGS = {"user_agent": NORMAL_USER_AGENT}

API_BASE = "https://api.zigzag.kr/api/2/graphql/"
HOME_URL = "https://zigzag.kr/"
LOGIN_URL = "https://zigzag.kr/auth/email-login?redirect=https://zigzag.kr/checkout/orders"
ORDERS_URL = "https://zigzag.kr/checkout/orders"
API_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://zigzag.kr",
    "Referer": "https://zigzag.kr/",
    "X-Requested-With": "XMLHttpRequest",
}

# 로그인이 없을 때 GraphQL이 주는 에러 코드/문구.
NOT_LOGGED_IN_CODE = "route_not_logged_in"
NOT_LOGGED_IN_MESSAGE = "not logged in"

# 주문목록은 한 번만(넉넉한 기간) 받아 캐시하고, '최근 우선'은 메모리에서 거른다.
# 예전에는 60일 -> 400일을 각각 API로 받아, 오래된 주문 하나 때문에 목록을 두 번
# 받았다(별도 캐시). 지그재그 목록은 볼륨이 작고(실측 300일 23건) 송장까지 다 들어
# 있어, 넓은 기간을 한 번 받아 두는 편이 요청도 적고 간단하다.
LOOKUP_DAYS = 400            # 실제로 받는 기간
RECENT_DAYS = 60            # 옵션이 겹칠 때 '최근 주문'을 먼저 보는 기준(메모리 필터)
LIST_MAX_PAGES = 40          # 커서 페이지네이션 상한(보통 1쪽에 끝난다)
MAX_RECIPIENT_LOOKUPS = 8    # 수령인 확인을 위해 여는 후보 수 상한(옥션과 같은 생각)

# 택배사 코드(shipping_company) -> 한글 이름. 사용자 요청 4·5번을 그대로 지킨다.
COURIER_CODE_MAP = {
    "CJ": "CJ대한통운",
    "LOTTE": "롯데택배",
    "LOGEN": "로젠택배",
    "HANJIN": "한진택배",
    "POSTAL": "우체국택배",
    "ILYANG": "일양로지스",
    "LOGIS": "합동택배",
}
DEFAULT_COURIER = "택배"

# 진행상태(영문 enum) 분류.
CANCELLED_KEYWORDS = ("CANCEL", "RETURN", "EXCHANGE")   # 취소/반품/교환 계열은 다시 올리지 않는다
DELAYED_STATES = ("SHIPPING_DEFERRED",)                 # 배송보류 = 발송지연
NOT_YET_STATES = (
    "PENDING", "BEFORE_TRANSFER", "NEW_ORDER",
    "SHIPMENT_PROCESS_REQUESTED", "AWAITING_SHIPMENT",
)
# 송장이 있을 법한 상태(발송 이후).
SHIPPED_STATES = (
    "SHIPPED", "IN_TRANSIT", "DOMESTIC_IN_TRANSIT",
    "INTERNATIONAL_IN_TRANSIT", "OVERSEAS_DIRECT_PURCHASE_IN_TRANSIT",
    "CONFIRMED",
)
# 사람이 읽는 상태 이름(메시지용). 화면 매핑을 그대로 옮겼다.
STATUS_LABELS = {
    "PENDING": "결제대기", "BEFORE_TRANSFER": "입금대기", "NEW_ORDER": "결제완료",
    "SHIPMENT_PROCESS_REQUESTED": "주문확인중", "AWAITING_SHIPMENT": "배송준비중",
    "IN_TRANSIT": "배송중", "DOMESTIC_IN_TRANSIT": "배송중", "SHIPPED": "배송완료",
    "SHIPPING_DEFERRED": "배송보류", "CONFIRMED": "구매확정",
    "RETURN_REQUESTED": "반품요청", "RETURN_COLLECTING": "반품수거중",
    "RETURN_COLLECTED": "반품수거완료", "RETURNED": "반품완료",
    "CANCELLED": "취소완료", "EXCHANGED": "교환완료",
}

# 주문목록 GraphQL 쿼리(필요한 필드만 골라 짧게 만들었다).
ORDER_ITEM_FIELDS = (
    "id order_item_number status item_status date_shipped shipping_company invoice_number "
    "shop_name quantity product_info { name options product_no } "
    "order_item_product { option_detail_list { name value } } "
    "order { order_number date_created id } "
    "shipping_group { shipping_type } fulfillment_info { date_expected_arrival_text } "
    "shipping_schedule_delay_info { recent_delay { delay_reason } } "
    "active_request_list { type status }"
)
ORDER_LIST_QUERY = (
    "query GetUserSearchedOrderList("
    "$status_list: [OrderItemStatus!] $last_id: ID $date_created_from: CrTimestamp! "
    "$date_created_to: CrTimestamp! $last_date_created: CrTimestamp) { "
    "user_searched_order_list(status_list: $status_list last_id: $last_id "
    "date_created_from: $date_created_from date_created_to: $date_created_to "
    "last_date_created: $last_date_created) { item_list { order_item_list { "
    + ORDER_ITEM_FIELDS + " } } has_next } }"
)
RECIPIENT_QUERY = (
    "query GetShippingGroupListForClaim($order_number: String) { "
    "shipping_group_list(order_number: $order_number) { item_list { "
    "order { order_number order_receiver { first_name last_name masked_name } } } } }"
)
ACCOUNT_QUERY = "query GetUserAccount { user_account { uuid } }"


@dataclass
class OrderItem:
    order_no: str
    order_date: date | None
    option: str                 # "(AD)DARK MELANGE GRAY / M_080"
    status: str                 # 영문 enum
    invoice_no: str             # 송장번호(없으면 "")
    courier_code: str           # 택배사 코드(없으면 "")
    delivery_note: str          # "내일(목) 이내 발송 예정" (없으면 "")
    delay_reason: str           # 지연 사유(없으면 "")
    has_return_request: bool     # active_request_list에 취소/반품/교환 요청이 있나

    @property
    def shipped(self) -> bool:
        return bool(self.invoice_no)


# 컨텍스트(이번 실행의 브라우저)별로 읽어둔 주문목록.
_listed: dict[int, list[OrderItem]] = {}
# 주문번호별 수령인(가려지지 않은 이름) - 같은 실행에서 두 번 묻지 않는다.
_recipients: dict[tuple[int, str], str] = {}
# 이번 get_tracking 한 건이 공급사 사이트에 실제 요청을 보냈는가. 목록을 미리
# 받아뒀고(prepare_batch) 그 캐시로만 답했으면 False - orchestrator가 요청 간격을
# 두지 않는다(갤러리아몰과 같은 sent_request 규칙). 수령인 조회나 목록 첫 로드처럼
# 실제로 API를 부른 경우에만 True.
_touched: dict[int, bool] = {}


# --------------------------------------------------------------------------
# 상품URL 해석
# --------------------------------------------------------------------------

def extract_order_no(product_url: str) -> str | None:
    """상품URL에 주문번호(숫자 12자리 이상)가 있으면 꺼낸다.

    쿼리(order_number/ord_no/orderNo)에도, 경로(/orders/<번호>)에도 올 수 있어
    둘 다 본다. 없으면 None - 주문옵션으로 찾는다.
    """
    parsed = urlparse(product_url)
    query = parse_qs(parsed.query)
    for key in ("order_number", "ord_no", "orderNo", "ordNo"):
        values = query.get(key)
        if values and values[0].strip().isdigit() and len(values[0].strip()) >= 12:
            return values[0].strip()
    for part in reversed(parsed.path.split("/")):
        part = part.strip()
        if part.isdigit() and len(part) >= 12:
            return part
    return None


# --------------------------------------------------------------------------
# GraphQL 요청 / 로그인
# --------------------------------------------------------------------------

def _post(context: BrowserContext, operation: str, query: str, variables: dict) -> dict:
    response = context.request.post(
        API_BASE + operation,
        data={"query": query, "variables": variables},
        headers=API_HEADERS,
    )
    try:
        return response.json()
    except Exception as e:  # noqa: BLE001 - JSON이 아니면 해석 실패로 묶는다
        raise ParseError(f"지그재그 {operation} 응답을 해석하지 못했습니다: {e}")


def _needs_login(body: dict) -> bool:
    for err in (body.get("errors") or []):
        code = str((err.get("extensions") or {}).get("code") or "")
        if code == NOT_LOGGED_IN_CODE or NOT_LOGGED_IN_MESSAGE in str(err.get("message") or ""):
            return True
    return False


def _human_type(page: Page, selector: str, text: str) -> None:
    page.click(selector)
    page.type(selector, text, delay=90)


def _auto_login(context: BrowserContext) -> None:
    """ZIGZAG_ID/ZIGZAG_PW로 자동 로그인하고 쿠키를 조회 컨텍스트로 옮긴다.

    로그인은 우리가 직접 실행해 CDP로 붙은 진짜 크롬 창에서만 한다 - 이유는
    이 파일 맨 위 docstring 참고. 홈을 잠깐 열어 봇 지문 쿠키를 만들고, 이메일
    로그인 폼에 사람처럼 한 글자씩 입력한 뒤 Enter를 친다. 성공/실패는 GraphQL
    user_account가 채워지는지로 본다(성공 화면이 /auth/를 벗어나는 것과 같다).
    """
    login_id = os.environ.get("ZIGZAG_ID")
    login_pw = os.environ.get("ZIGZAG_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "지그재그 로그인이 필요하지만 ZIGZAG_ID/ZIGZAG_PW 환경변수가 설정되어 있지 "
            "않습니다. .env에 추가해주세요.")

    try:
        with browser_mod.real_chrome_cdp_context(SITE_KEY) as login_context:
            page = login_context.pages[0] if login_context.pages else login_context.new_page()
            page.set_viewport_size(browser_mod.MOBILE_VIEWPORT)
            alerts: list[str] = []
            page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))

            # 프로필에 로그인이 살아 있으면 자격 증명을 넣지 않고 쿠키만 옮긴다.
            if _logged_in(login_context):
                _copy_cookies(context, login_context)
                common.safe_print("[zigzag] 크롬 프로필에 남아 있던 로그인 세션을 옮겼습니다.")
                return

            # 봇 지문 쿠키(ZIGZAG_FINGERPRINT)를 만들려고 홈을 잠깐 연다.
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_selector("input[type='password']", state="visible", timeout=20000)
            page.wait_for_timeout(5000)   # 봇 점수가 자리잡을 시간
            _human_type(page, "input[type='email']", login_id)
            page.wait_for_timeout(600)
            _human_type(page, "input[type='password']", login_pw)
            page.wait_for_timeout(1000)
            page.keyboard.press("Enter")

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                page.wait_for_timeout(400)
                if "/auth/error" in page.url or alerts:
                    hint = f" 사이트 안내: {alerts[-1]}" if alerts else ""
                    raise BlockedError(f"지그재그가 로그인을 거부했습니다.{hint}")
                if "/auth/" not in page.url:
                    _copy_cookies(context, login_context)
                    common.safe_print("[zigzag] 자동 로그인에 성공했습니다.")
                    return
            raise BlockedError("지그재그 자동 로그인 결과를 30초 안에 확인하지 못했습니다.")
    except RuntimeError as exc:  # 크롬 미설치, 디버깅 포트가 안 열림 등
        raise BlockedError(
            f"지그재그 로그인용 크롬 창을 띄우지 못했습니다({exc}) - 이 사이트는 봇 확인 때문에 "
            "설치된 진짜 크롬으로만 로그인할 수 있습니다.") from exc


def _logged_in(context: BrowserContext) -> bool:
    body = _post(context, "GetUserAccount", ACCOUNT_QUERY, None)
    return bool((body.get("data") or {}).get("user_account"))


def _copy_cookies(context: BrowserContext, login_context: BrowserContext) -> None:
    """로그인 창의 쿠키를 조회 컨텍스트로 옮긴다 - 조회 쪽 zigzag 쿠키는 먼저 지운다."""
    import re
    context.clear_cookies(domain=re.compile(r"zigzag\.kr$"))
    context.add_cookies(login_context.cookies())


# --------------------------------------------------------------------------
# 주문목록 읽기
# --------------------------------------------------------------------------

def _to_date(ms) -> date | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).date()
    except Exception:  # noqa: BLE001
        return None


def _parse_item(raw: dict) -> OrderItem:
    order = raw.get("order") or {}
    product = raw.get("product_info") or {}
    details = (raw.get("order_item_product") or {}).get("option_detail_list") or []
    option = str(product.get("options") or "").strip()
    if not option and details:
        option = " / ".join(str(d.get("value") or "").strip() for d in details if d.get("value"))
    ff = raw.get("fulfillment_info") or {}
    delay = ((raw.get("shipping_schedule_delay_info") or {}).get("recent_delay") or {})
    requests = raw.get("active_request_list") or []
    return OrderItem(
        order_no=str(order.get("order_number") or ""),
        order_date=_to_date(order.get("date_created")),
        option=option,
        status=str(raw.get("status") or raw.get("item_status") or "").upper(),
        invoice_no="".join(ch for ch in str(raw.get("invoice_number") or "") if ch.isdigit()),
        courier_code=str(raw.get("shipping_company") or "").strip().upper(),
        delivery_note=str(ff.get("date_expected_arrival_text") or "").strip(),
        delay_reason=str(delay.get("delay_reason") or "").strip(),
        has_return_request=any(
            str(r.get("type") or "").upper() in ("RETURN", "CANCEL", "EXCHANGE") for r in requests),
    )


def _fetch_list(context: BrowserContext, days: int) -> list[OrderItem]:
    """오늘부터 days일 전까지의 주문목록을 커서 페이지네이션으로 전부 받는다."""
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - days * 86400000
    items: list[OrderItem] = []
    last_id = None
    last_date_created = None
    for _ in range(LIST_MAX_PAGES):
        variables = {
            "status_list": None,
            "last_id": last_id,
            "date_created_from": from_ms,
            "date_created_to": now_ms,
            "last_date_created": last_date_created,
        }
        _touched[id(context)] = True  # 목록을 실제로 API로 받는다
        body = _post(context, "GetUserSearchedOrderList", ORDER_LIST_QUERY, variables)
        if _needs_login(body):
            _auto_login(context)
            body = _post(context, "GetUserSearchedOrderList", ORDER_LIST_QUERY, variables)
            if _needs_login(body):
                raise BlockedError("지그재그 로그인 후에도 주문목록을 받지 못했습니다 (로그인 필요).")
        data = ((body.get("data") or {}).get("user_searched_order_list") or {})
        groups = data.get("item_list") or []
        raw_items = [ri for g in groups for ri in (g.get("order_item_list") or [])]
        if not raw_items:
            break
        for ri in raw_items:
            items.append(_parse_item(ri))
        if not data.get("has_next"):
            break
        last_raw = raw_items[-1]
        last_id = last_raw.get("id")
        last_date_created = (last_raw.get("order") or {}).get("date_created")
        if last_id is None:
            break
    return items


def _all_items(context: BrowserContext) -> list[OrderItem]:
    """주문목록 전체(최근 LOOKUP_DAYS일). 실행 중 한 번만 받아 캐시한다."""
    key = id(context)
    if key not in _listed:
        _listed[key] = _fetch_list(context, LOOKUP_DAYS)
        common.safe_print(f"[zigzag] 최근 {LOOKUP_DAYS}일 주문목록 {len(_listed[key])}건을 읽었습니다.")
    return _listed[key]


def _recent(items: list[OrderItem]) -> list[OrderItem]:
    """최근 RECENT_DAYS일 안에 주문한 줄만 (옵션이 겹칠 때 먼저 보는 후보)."""
    floor = date.fromordinal(date.today().toordinal() - RECENT_DAYS)
    return [it for it in items if it.order_date and it.order_date >= floor]


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이 공급사의 첫 조회 전에 주문목록을 한 번 받아 캐시한다 (갤러리아몰과 같은 방식).

    orchestrator가 사이트별로 한 번 불러준다. 목록에는 송장번호까지 다 들어 있어,
    이 뒤의 조회는 대부분 캐시로만 답하고 실제 요청을 보내지 않는다(sent_request=False)
    - orchestrator가 요청 간격(0.5~1.2초)을 두지 않는다. 실패해도 조회 때 지연
    로딩되므로 어떤 예외도 밖으로 내보내지 않는다. 세션이 없으면 여기서 로그인까지
    끝나 첫 조회가 빨라진다.
    """
    try:
        _all_items(context)
    except Exception as e:  # noqa: BLE001 - 미리 읽기는 실패해도 조회 경로가 받아준다
        common.safe_print(f"[zigzag] 주문목록 미리 읽기 실패 - 주문마다 조회합니다: {e}")


def _recipient_of(context: BrowserContext, order_no: str) -> str:
    """주문번호의 수령인(가려지지 않은 전체 이름). 못 읽으면 ''."""
    key = (id(context), order_no)
    if key not in _recipients:
        _touched[id(context)] = True  # 수령인 조회는 실제 API 요청이다
        body = _post(context, "GetShippingGroupListForClaim", RECIPIENT_QUERY, {"order_number": order_no})
        name = ""
        groups = ((body.get("data") or {}).get("shipping_group_list") or {}).get("item_list") or []
        for g in groups:
            receiver = (g.get("order") or {}).get("order_receiver") or {}
            name = str(receiver.get("first_name") or receiver.get("masked_name") or "").strip()
            if name:
                break
        _recipients[key] = name
    return _recipients[key]


# --------------------------------------------------------------------------
# 상태 판정 / 결과 만들기
# --------------------------------------------------------------------------

def _label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "없음")


def _map_courier(code: str) -> str:
    if code in COURIER_CODE_MAP:
        return COURIER_CODE_MAP[code]
    return common.normalize_courier(code) if code else DEFAULT_COURIER


def _result_for_item(context: BrowserContext, item: OrderItem) -> TrackingResult:
    """주문 한 줄의 결론. sent_request는 이번 조회가 실제 요청을 보냈는지(_touched)로 정한다."""
    sent_request = _touched.get(id(context), True)
    order_no = item.order_no
    status = item.status
    is_claim = any(k in status for k in CANCELLED_KEYWORDS) or item.has_return_request
    if is_claim and not item.shipped:
        e = OrderCancelled(
            f"취소/반품/교환된 주문으로 보입니다 (주문번호={order_no}, 상태={_label(status)}).")
        e.sent_request = sent_request
        raise e
    if item.shipped and not is_claim:
        return TrackingResult(
            tracking_no=item.invoice_no,
            courier=_map_courier(item.courier_code),
            delivery_note=eta_mod.from_text(item.delivery_note) or (item.delivery_note or None),
            sent_request=sent_request)
    if is_claim:  # 송장은 있지만 취소/반품/교환 진행 중 - 올리면 안 된다
        e = OrderCancelled(
            f"취소/반품/교환이 진행 중인 주문입니다 (주문번호={order_no}, 상태={_label(status)}).")
        e.sent_request = sent_request
        raise e
    # 아직 송장이 없다 - 지연이면 그쪽으로, 아니면 미발급.
    if status in DELAYED_STATES or item.delay_reason:
        reason = item.delay_reason or _label(status)
        e = ShipmentDelayed(
            f"공급사가 발송지연('{reason}')을 알린 주문이라 아직 발송 전으로 봅니다 (주문번호={order_no}).")
        e.sent_request = sent_request
        raise e
    e = TrackingNotAvailableYet(
        f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={_label(status)}).")
    e.sent_request = sent_request
    raise e


# --------------------------------------------------------------------------
# 주문 고르기 (사용자 요청 6번)
# --------------------------------------------------------------------------

def _row_is_dead(item: OrderItem) -> bool:
    return (any(k in item.status for k in CANCELLED_KEYWORDS) or item.has_return_request) and not item.shipped


def _candidates(items: list[OrderItem], order_option: str | None) -> list[tuple[int, OrderItem]]:
    """옵션 점수가 0보다 큰 (점수, 주문줄)을 점수 높은 순으로."""
    scored: list[tuple[int, int, OrderItem]] = []
    for position, item in enumerate(items):
        if _row_is_dead(item):
            continue
        score = option_score(order_option, item.option) if order_option else 0
        if score > 0:
            scored.append((score, position, item))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(s, it) for s, _, it in scored]


def _select_by_option(items: list[OrderItem], order_option: str | None) -> OrderItem | None:
    """한 주문 안에 상품 줄이 여럿일 때 옵션으로 하나를 고른다(이랜드몰과 같은 규칙)."""
    alive = [it for it in items if not _row_is_dead(it)]
    if len(alive) == 1:
        return alive[0]
    if not order_option or not alive:
        return None
    target = normalize_option(order_option)
    if target:
        contained = [it for it in alive if target in normalize_option(it.option)]
        if len(contained) == 1:
            return contained[0]
    scored = sorted(((option_score(order_option, it.option), i) for i, it in enumerate(alive)),
                    reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return alive[scored[0][1]]
    return None


def _find_by_option(context: BrowserContext, order_option: str | None,
                    recipient_name: str | None) -> OrderItem:
    """주문목록에서 옵션(+수령인)으로 어느 주문의 어느 줄인지 찾는다.

    최근 60일 주문에서 먼저 찾고, 없으면 전체(400일)로 넓힌다 - 목록은 한 번만
    받아두고 메모리에서 거른다(갤러리아몰과 같은 '최근 우선' 생각).
    """
    if not order_option:
        raise ParseError(
            "지그재그 상품URL에 주문번호가 없어 주문옵션으로 찾아야 하는데 주문옵션이 비어 "
            "있습니다. 샵마인 내보내기에 '주문옵션'/'수령인' 컬럼을 포함해주세요.")

    listed = _all_items(context)
    candidates = _candidates(_recent(listed), order_option) or _candidates(listed, order_option)
    if not candidates:
        raise OrderNotFound(
            f"주문옵션 {order_option!r}과 맞는 주문을 최근 주문내역({len(listed)}건)에서 찾지 못했습니다.")

    if recipient_name:
        # 옵션이 같은 주문이 여러 고객에게 있는 것이 보통이라, 점수 높은 순서대로
        # 수령인이 같은 주문을 고른다 (옥션·갤러리아몰과 같은 규칙).
        checked = 0
        for _, item in candidates[:MAX_RECIPIENT_LOOKUPS]:
            checked += 1
            if recipient_matches(_recipient_of(context, item.order_no), recipient_name):
                return item
        raise ParseError(
            f"주문옵션 {order_option!r}으로 찾은 후보 {checked}건 중 수령인이 {recipient_name!r}인 "
            "주문이 없습니다 - 지그재그에서 직접 확인해주세요.")

    best = candidates[0][0]
    tied = [it for score, it in candidates if score == best]
    if len(tied) > 1:
        raise ParseError(
            f"주문옵션 {order_option!r}에 똑같이 들어맞는 주문이 {len(tied)}건이라 어느 주문인지 확정할 수 "
            f"없습니다 (주문번호: {', '.join(it.order_no for it in tied[:5])}). "
            "샵마인 내보내기에 '수령인' 컬럼을 포함하면 자동으로 구분됩니다.")
    return tied[0]


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _lookup_by_order_no(context: BrowserContext, order_no: str,
                        order_option: str | None) -> TrackingResult:
    rows = [it for it in _all_items(context) if it.order_no == order_no]
    if not rows:
        raise OrderNotFound(
            f"지그재그 주문내역에서 주문번호 {order_no}을(를) 찾지 못했습니다 (없는 주문이거나 조회기간 밖).")

    # 주문일은 그 주문의 아무 줄에서나 같으므로 첫 줄에서 읽어 결과에 실어준다.
    found_date = next((it.order_date for it in rows if it.order_date), None)

    row = _select_by_option(rows, order_option)
    if row is not None:
        return attach_order_date(found_date, lambda: _result_for_item(context, row))

    # 옵션으로 특정할 수 없으면 발송된 줄을 전부 보고 송장이 하나뿐인지 본다
    # (다른 어댑터와 같은 안전 규칙). 발송된 줄이 없으면 상태만 판정한다.
    shipped = [it for it in rows if it.shipped and not _row_is_dead(it)]
    if not shipped:
        alive = [it for it in rows if not _row_is_dead(it)]
        return attach_order_date(found_date, lambda: _result_for_item(context, alive[0] if alive else rows[0]))
    results = [_result_for_item(context, it) for it in shipped]
    if len({r.tracking_no for r in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "주문옵션으로 어느 상품인지 고르지 못했습니다. 상품별로 나눠 배송된 것으로 보입니다.")
    result = results[0]
    if result.order_date is None:
        result.order_date = found_date
    return result


def get_tracking(
    context: BrowserContext,
    product_url: str,
    headless: bool = True,
    order_option: str | None = None,
    recipient_name: str | None = None,
) -> TrackingResult:
    _touched[id(context)] = False  # 이번 조회가 실제 요청을 보냈는지 여기서부터 센다
    order_no = extract_order_no(product_url)
    if order_no:
        return _lookup_by_order_no(context, order_no, order_option)

    item = _find_by_option(context, order_option, recipient_name)
    return attach_order_date(item.order_date, lambda: _result_for_item(context, item))
