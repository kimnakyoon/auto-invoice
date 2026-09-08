"""이랜드몰(www.elandmall.co.kr) 공급사 어댑터.

리버스엔지니어링 결과 (2026-09-08 실측):
- 샵마인 엑셀의 "상품URL"에는 주문별 상세 주소가 들어온다:
    https://www.elandmall.co.kr/m/order-dtl?ordNo=<주문번호>&pageId=...&preCornerNo=...
  그런데 신세계TV쇼핑과 똑같이 **주문상세 화면에는 배송조회가 없다** - 진행현황
  글자(배송준비중/배송완료)만 있다. 송장번호와 [배송현황조회]는 **주문목록**
  (/m/order-list)의 상품 행에만 나온다. 그래서 이 어댑터는 상세 대신 목록을 읽는다.
- 목록도 배송현황 레이어도 서버가 그려서 내려주는 HTML이라 **브라우저 화면을
  열지 않고 context.request로 받는다**. 페이지는 로그인이 필요할 때만 잠깐 연다.
- 주문번호는 앞 8자리가 주문일(YYYYMMDD)이다 (202609089061801 -> 2026-09-08,
  목록의 주문일과 일치 - 6개월치 17건 전부 확인). 목록은 조회기간을 주소로 받는다:
    /m/order-list?page=1&searchStartDate=YYYYMMDD&searchEndDate=YYYYMMDD
  **그 날 하루**로 좁히면 그 주문만 딱 나온다(실측). 페이지당 10건이고 화면 JS에
  `var lastPage = Math.ceil(<총건수> / 10)` 이 박혀 있어 페이지 수를 안다. 조회
  기간은 최대 6개월(화면 JS가 막는다).
- 이번에 조회할 주문이 2건 이상이면 prepare_batch가 가장 오래된 주문일부터
  오늘까지의 목록을 **한 번** 받아 전부 캐시해 둔다 - 캐시만으로 결론이 나는
  주문은 요청을 안 보냈으므로 오케스트레이터가 간격도 두지 않는다(sent_request=False).
- 목록 HTML의 주문 표는 div.order-item 안의 table 하나이고, 주문마다
  <tr><td class="order-info__list"> (주문번호 링크 /m/order-dtl?ordNo=…, "주문일 :
  2026-08-29") 가 먼저, 그 뒤에 상품 한 줄씩 <tr data-cart-no="…"> 가 온다. 줄마다
    - p.order_option  "옵션 : 아이보리/S(FREE)"
    - 2번째 td        배송정보 - 발송된 줄에만 [배송현황조회] 링크가 있고, 그 링크의
                     data-shipstate_layer='{"ordNo","ordDetailNo","invoiceNo":"508274306316",
                     "couriercoDcode":"12", ...}' 에 **송장번호가 그대로 들어 있다**
    - 3번째 td        진행상태 - 실측한 값: 배송준비중 / 배송완료 / 취소완료 / 반품완료 / 교환완료
- 택배사 이름은 목록에 없고 [배송현황조회]가 여는 레이어
  (/m/delistat-layer?ordNo=…&ordDetailNo=…, 서버 렌더 HTML)에 <th>택배업체</th><td>CJ대한통운</td>
  꼴로 있다. 같은 실행 안에서는 택배사 코드(couriercoDcode)별로 한 번만 받아 캐시한다
  (GSSHOP과 같은 방식). 실측: 12 = CJ대한통운, 11 = 롯데택배. 마지막에
  common.normalize_courier를 거쳐 CJ/대한통운 -> CJ대한통운, 롯데 -> 롯데택배,
  DELIBOX -> 딜리박스로 맞춘다 (사용자 요청 4·5번).
- 로그인이 안 되어 있으면 목록/레이어 요청이 /m/login?returnUrl=… 으로 302된다
  (request로 받아도 최종 주소가 그렇게 바뀌어 있어 같은 기준으로 판정한다). 주문상세는
  로그인 없이 열면 302가 아니라 500 오류 화면이라 판정에 못 쓴다.
  로그인 화면(/m/login)은 기본으로 간편 로그인 탭이 열려 있고 아이디/비밀번호 폼은
  "일반 로그인" 탭(a[login-page-tab]) 안에 숨어 있다. 입력창은 id가 없고
  data-login-id="userId"/"pwd", 버튼은 [data-login-btn]. 폼이 페이지 안에 두 벌
  (상단 레이어용/본문용) 있어 보이는 것만 쓴다. 제출하면 reCAPTCHA v3 토큰을 받아
  POST /v1/login 으로 가는데, **번들 크로미엄 headless로도 통과했다**(창 안 뜸).
  실패는 alert(resultMessage)로 온다. 사용자가 "쿠키로 첫 로그인부터 자동"을 요청해서
  ELANDMALL_ID/ELANDMALL_PW로 완전 자동 로그인하고, 세션은 storage_state
  (auth/elandmall_state.json)로 저장돼 다음 실행부터는 쿠키만으로 바로 조회된다.

주문에 상품이 여러 줄이면 (사용자 요청 6번: "주문 옵션을 비교해서 찾아줘"):
  1. 샵마인 엑셀의 "주문옵션"이 어느 줄의 옵션에만 유일하게 들어 있으면 그 줄
  2. 아니면 옥션과 같은 토큰 점수(auction.option_score)로 1등이 유일하면 그 줄
  3. 그래도 못 고르면 발송된 줄을 전부 보고 송장이 하나뿐이면 그것, 서로 다르면
     사람이 보도록 ParseError
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from ..models import TrackingResult
from . import common
from .auction import option_score
from .base import (
    AdapterError,
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
)

load_dotenv()

DOMAINS = {"elandmall.co.kr", "www.elandmall.co.kr", "m.elandmall.co.kr"}
SITE_KEY = "elandmall"

# 화면 없이 가벼운 HTML 요청만 보내고 봇 확인도 없는 사이트 - 신세계TV쇼핑과 같은
# 간격. 캐시로 답한 주문에는 간격 자체가 안 붙는다.
REQUEST_GAP = (0.5, 1.2)

BASE_URL = "https://www.elandmall.co.kr"
LIST_URL = BASE_URL + "/m/order-list?page={page}&searchStartDate={from_date}&searchEndDate={to_date}"
LAYER_URL = BASE_URL + "/m/delistat-layer?ordNo={order_no}&ordDetailNo={detail_no}"
LOGIN_URL = BASE_URL + "/m/login?returnUrl=/m/order-list"
LOGIN_PATH = "/m/login"

LOGIN_TAB_SELECTOR = "a[login-page-tab]:visible"
LOGIN_TAB_TEXT = "일반 로그인"
LOGIN_ID_SELECTOR = '[data-login-id="userId"]:visible'
LOGIN_PW_SELECTOR = '[data-login-id="pwd"]:visible'
LOGIN_BUTTON_SELECTOR = "[data-login-btn]:visible"
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000

# 목록은 페이지당 10건 고정. 사이트가 허용하는 조회기간 상한(6개월)보다 조금
# 안쪽으로 잡는다 - 그보다 오래된 주문은 어차피 송장 조회 대상이 아니다.
LIST_ROWS_PER_PAGE = 10
LIST_MAX_PAGES = 10
LIST_MAX_DAYS = 6 * 30 - 1
# 1건이면 날짜로 좁힌 목록이나 기간 목록이나 요청 하나라 이득이 없다.
LIST_PREFETCH_MIN_ORDERS = 2

ORDER_NO_PREFIX_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})\d+$")
LAST_PAGE_PATTERN = re.compile(r"var lastPage = Math\.ceil\((\d+) / (\d+)\)")
ROW_PATTERN = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
CELL_PATTERN = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S)
ORDER_NO_PATTERN = re.compile(r"order-dtl\?ordNo=(\d+)")
ORDER_DATE_PATTERN = re.compile(r"주문일\s*:\s*(\d{4}-\d{2}-\d{2})")
OPTION_PATTERN = re.compile(r'class="order_option"[^>]*>(.*?)</p>', re.S)
SHIP_DATA_PATTERN = re.compile(r"data-shipstate_layer='([^']*)'")
OPTION_LABEL_PATTERN = re.compile(r"^옵션\s*:\s*")
LAYER_FIELD_PATTERN = re.compile(r'<th scope="row">(.*?)</th>\s*<td[^>]*>(.*?)</td>', re.S)
TRACKING_PATTERN = re.compile(r"\d{9,}")

ORDER_TABLE_MARKER = 'class="order-item"'
ORDER_HEADER_MARKER = "order-info__list"
ITEM_ROW_MARKER = "data-cart-no"

COURIER_LABEL = "택배업체"
TRACKING_LABEL = "송장번호"
DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값

# 반품이 끝난 줄은 송장이 있어도 다시 보낼 것이 없고, 없으면 영영 안 나온다.
RETURNED_KEYWORD = "반품"


@dataclass
class OrderRow:
    option: str        # "아이보리/S(FREE)" (라벨 '옵션 :'은 뗀 값)
    state: str         # "배송준비중" / "배송완료" / "취소완료" / ...
    ship: dict | None  # [배송현황조회] 링크의 JSON. 없으면(아직 발송 전) None


@dataclass
class ListedOrder:
    order_no: str
    order_date: date | None
    rows: list[OrderRow] = field(default_factory=list)


# prepare_batch가 읽어둔 목록과, 택배사 코드별 이름. 컨텍스트(=이번 실행의
# 브라우저)별로 담는다. 한 공급사는 스레드 하나가 맡으므로 잠금은 필요 없다.
_listed_orders: dict[int, dict[str, ListedOrder]] = {}
_courier_names: dict[int, dict[str, str]] = {}


def extract_order_no(product_url: str) -> str:
    values = parse_qs(urlparse(product_url).query).get("ordNo")
    if not values or not values[0].strip():
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0].strip()


def order_date_from_no(order_no: str) -> date | None:
    """주문번호 앞 8자리가 날짜면 그 날짜 (아니면 None)."""
    m = ORDER_NO_PREFIX_DATE.match(order_no)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 요청 / 로그인
# --------------------------------------------------------------------------

def _is_login_url(url: str) -> bool:
    return LOGIN_PATH in url


def _auto_login(page: Page) -> None:
    """ELANDMALL_ID/ELANDMALL_PW로 완전 자동 로그인한다 (사용자 명시 요청)."""
    login_id = os.environ.get("ELANDMALL_ID")
    login_pw = os.environ.get("ELANDMALL_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "이랜드몰 로그인이 필요하지만 ELANDMALL_ID/ELANDMALL_PW 환경변수가 "
            "설정되어 있지 않습니다. .env에 추가해주세요.")

    # 실패는 alert(resultMessage)로 온다 - 붙잡아 두었다가 사유로 쓴다.
    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    # 입력창은 window load 뒤에야 보이게 그려진다(JS가 그 전까지 visibility를
    # 숨긴다). 그리고 아이디/비밀번호 폼은 "일반 로그인" 탭 안에 접혀 있다.
    page.wait_for_load_state("load")
    tab = page.locator(LOGIN_TAB_SELECTOR).filter(has_text=LOGIN_TAB_TEXT)
    if tab.count():
        tab.first.click()
    page.locator(LOGIN_ID_SELECTOR).first.fill(login_id)
    page.locator(LOGIN_PW_SELECTOR).first.fill(login_pw)
    page.locator(LOGIN_BUTTON_SELECTOR).first.click()
    left = common.wait_for_url(page, lambda url: not _is_login_url(url), LOGIN_WAIT_TIMEOUT_MS,
                               poll_ms=500)
    if not left:
        reason = f" 사이트 안내: {alerts[-1]}" if alerts else ""
        raise BlockedError(f"이랜드몰 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.{reason}")


def _login_with_page(context: BrowserContext) -> None:
    """페이지를 잠깐 열어 자동 로그인한다 - 세션이 없을 때만 오는 느린 경로."""
    common.safe_print("[elandmall] 로그인 세션이 없어 자동 로그인을 시도합니다.")
    page = context.new_page()
    try:
        common.goto_settled(page, LOGIN_URL)
        if not _is_login_url(page.url):
            return  # 요청 시점과 달리 지금은 로그인이 살아 있다
        _auto_login(page)
    finally:
        page.close()


def _get_html(context: BrowserContext, url: str) -> str:
    """로그인된 상태로 HTML을 받는다. 세션이 없으면 한 번 로그인하고 다시 받는다."""
    response = context.request.get(url)
    if not _is_login_url(response.url):
        return response.text()
    _login_with_page(context)
    response = context.request.get(url)
    if _is_login_url(response.url):
        raise BlockedError("이랜드몰 로그인 후에도 여전히 로그인 페이지입니다.")
    return response.text()


# --------------------------------------------------------------------------
# 주문목록 HTML 해석
# --------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))).strip()


def _strip_option_label(text: str) -> str:
    return OPTION_LABEL_PATTERN.sub("", text or "").strip()


def _parse_list_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_ship_data(row_html: str) -> dict | None:
    m = SHIP_DATA_PATTERN.search(row_html)
    if not m:
        return None
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_orders(html: str) -> list[ListedOrder]:
    """주문목록 HTML에서 주문(헤더 행)마다 상품 줄(data-cart-no 행)을 뽑는다."""
    start = html.find(ORDER_TABLE_MARKER)
    if start < 0:
        return []
    end = html.find("</table>", start)
    orders: list[ListedOrder] = []
    current: ListedOrder | None = None
    for row_html in ROW_PATTERN.findall(html[start:end if end > 0 else None]):
        if ORDER_HEADER_MARKER in row_html:
            no_match = ORDER_NO_PATTERN.search(row_html)
            if not no_match:
                current = None
                continue
            date_match = ORDER_DATE_PATTERN.search(_strip_tags(row_html))
            current = ListedOrder(
                order_no=no_match.group(1),
                order_date=_parse_list_date(date_match.group(1)) if date_match else None)
            orders.append(current)
            continue
        if current is None or ITEM_ROW_MARKER not in row_html:
            continue
        cells = CELL_PATTERN.findall(row_html)
        option_match = OPTION_PATTERN.search(row_html)
        current.rows.append(OrderRow(
            option=_strip_option_label(_strip_tags(option_match.group(1))) if option_match else "",
            state=_strip_tags(cells[2]) if len(cells) > 2 else "",
            ship=_parse_ship_data(row_html)))
    return orders


def _total_pages(html: str) -> int:
    m = LAST_PAGE_PATTERN.search(html)
    if not m:
        return 1
    total, per_page = int(m.group(1)), int(m.group(2)) or LIST_ROWS_PER_PAGE
    return max(1, -(-total // per_page))


def _fetch_list(context: BrowserContext, from_date: date, to_date: date) -> dict[str, ListedOrder]:
    """기간 안의 목록을 페이지가 끝날 때까지 받아 주문번호별로 모은다."""
    found: dict[str, ListedOrder] = {}
    ymd_from, ymd_to = from_date.strftime("%Y%m%d"), to_date.strftime("%Y%m%d")
    pages = 1
    page_no = 1
    while page_no <= min(pages, LIST_MAX_PAGES):
        html = _get_html(context, LIST_URL.format(page=page_no, from_date=ymd_from, to_date=ymd_to))
        if page_no == 1:
            pages = _total_pages(html)
        orders = parse_orders(html)
        if not orders:
            break
        for order in orders:
            found.setdefault(order.order_no, order)
        page_no += 1
    return found


def _full_range(today: date, oldest: date | None) -> tuple[date, date]:
    """가장 오래된 주문일(모르면 상한)부터 오늘까지 - 사이트 상한 6개월 안에서."""
    floor = today - timedelta(days=LIST_MAX_DAYS)
    start = oldest if oldest is not None and oldest > floor else floor
    return start, today


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문이 2건 이상이면 기간 목록을 한 번 받아 캐시한다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 실패하면 아무것도
    읽지 않은 것과 같아서 모든 주문이 주문별 목록 경로로 간다 - 그래서 어떤
    예외도 밖으로 내보내지 않는다.
    """
    wanted: set[str] = set()
    for order in orders:
        try:
            wanted.add(extract_order_no(order.product_url))
        except ParseError:
            continue
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return
    dates = [d for d in (order_date_from_no(no) for no in wanted) if d is not None]
    today = date.today()
    # 주문번호에서 날짜를 못 읽은 주문이 하나라도 있으면 상한까지 다 받는다.
    oldest = min(dates) if dates and len(dates) == len(wanted) else None
    try:
        listed = _fetch_list(context, *_full_range(today, oldest))
    except Exception as e:  # noqa: BLE001 - 미리 읽기는 실패해도 주문별 경로가 있다
        common.safe_print(f"[elandmall] 주문목록 미리 읽기 실패 - 주문별로 조회합니다: {e}")
        return
    _listed_orders[id(context)] = listed
    hit = len(wanted & set(listed))
    common.safe_print(f"[elandmall] 주문목록 {len(listed)}건을 미리 읽었습니다 "
                      f"- 조회 대상 {len(wanted)}건 중 {hit}건이 목록에 있습니다.")


def _locate_order(context: BrowserContext, order_no: str) -> tuple[ListedOrder, bool]:
    """주문번호로 그 주문의 상품 줄들을 얻는다. (주문, 요청을 보냈는가)"""
    cached = _listed_orders.get(id(context), {}).get(order_no)
    if cached is not None:
        return cached, False

    found_date = order_date_from_no(order_no)
    if found_date is not None:
        order = _fetch_list(context, found_date, found_date).get(order_no)
        if order is not None:
            return order, True

    order = _fetch_list(context, *_full_range(date.today(), None)).get(order_no)
    if order is not None:
        return order, True
    raise OrderNotFound(f"주문내역(최근 6개월)에서 주문번호 {order_no}을(를) 찾지 못했습니다.")


# --------------------------------------------------------------------------
# 상품 줄 고르기 (사용자 요청 6번)
# --------------------------------------------------------------------------

def select_row(rows: list[OrderRow], order_option: str | None) -> OrderRow | None:
    """샵마인 "주문옵션"으로 상품 줄 하나를 고른다. 못 고르면 None.

    1) 정규화한 옵션이 그대로 들어 있는 줄이 유일하면 그 줄
    2) 아니면 토큰 점수(옥션과 같은 방식) 1등이 유일하고 0점이 아니면 그 줄
    """
    if len(rows) == 1:
        return rows[0]
    if not order_option:
        return None
    target = normalize_option(order_option)
    if target:
        contained = [r for r in rows if target in normalize_option(r.option)]
        if len(contained) == 1:
            return contained[0]
    scored = sorted(((option_score(order_option, r.option), i) for i, r in enumerate(rows)),
                    reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return rows[scored[0][1]]
    return None


# --------------------------------------------------------------------------
# 택배사 이름 (배송현황 레이어)
# --------------------------------------------------------------------------

def _layer_fields(html: str) -> dict[str, str]:
    """레이어의 <th>라벨</th><td>값</td> 쌍."""
    return {_strip_tags(label): _strip_tags(value) for label, value in LAYER_FIELD_PATTERN.findall(html)}


def courier_name(context: BrowserContext, ship: dict, order_no: str) -> tuple[str, bool]:
    """이 줄의 택배사 이름. (이름, 이번에 레이어 요청을 보냈는가)

    같은 택배사 코드는 실행 중 한 번만 레이어를 받는다. 처음 받을 때는 레이어의
    송장번호가 목록의 것과 같은지 검산해서 엉뚱한 줄을 읽지 않았는지 확인한다.
    """
    code = str(ship.get("couriercoDcode") or "")
    cache = _courier_names.setdefault(id(context), {})
    if code and code in cache:
        return cache[code], False
    detail_no = ship.get("ordDetailNo")
    if not detail_no:
        return DEFAULT_COURIER, False
    html = _get_html(context, LAYER_URL.format(order_no=order_no, detail_no=detail_no))
    fields = _layer_fields(html)
    if COURIER_LABEL not in fields and TRACKING_LABEL not in fields:
        raise ParseError(f"배송현황 레이어 구조를 해석하지 못했습니다 (주문번호={order_no}, 상세번호={detail_no}).")
    layer_tracking = re.sub(r"\D", "", fields.get(TRACKING_LABEL, ""))
    list_tracking = re.sub(r"\D", "", str(ship.get("invoiceNo") or ""))
    if layer_tracking and list_tracking and layer_tracking != list_tracking:
        raise ParseError(
            f"배송현황 레이어의 송장번호({layer_tracking})가 목록의 송장번호({list_tracking})와 "
            f"다릅니다 (주문번호={order_no}, 상세번호={detail_no}).")
    name = fields.get(COURIER_LABEL, "").strip()
    name = common.normalize_courier(name) if name else DEFAULT_COURIER
    if code:
        cache[code] = name
    return name, True


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _raise_for_unshipped(row: OrderRow, order_no: str) -> None:
    """송장이 없는 줄의 결론. 취소/반품이면 OrderCancelled, 아니면 미발급."""
    raise_if_cancelled(row.state, order_no)
    if RETURNED_KEYWORD in row.state:
        raise OrderCancelled(
            f"주문 화면에 '{row.state}' 표시가 있습니다 (주문번호={order_no}) - 반품된 주문인지 확인해주세요.")
    raise TrackingNotAvailableYet(
        f"아직 배송현황조회가 열리지 않은 주문입니다 (주문번호={order_no}, 상태={row.state or '없음'}).")


def _lookup(context: BrowserContext, order: ListedOrder, order_option: str | None,
            sent_request: bool) -> TrackingResult:
    order_no = order.order_no
    rows = order.rows
    if not rows:
        raise ParseError(f"주문목록에서 상품 줄을 읽지 못했습니다 (주문번호={order_no}).")

    row = select_row(rows, order_option)
    if row is not None:
        candidates = [row]
    else:
        # 옵션으로 특정할 수 없으면 발송된 줄을 전부 보고 송장이 하나뿐인지 본다
        # (다른 어댑터와 같은 안전 규칙). 발송된 줄이 없으면 전부를 상태 판정에 쓴다.
        shipped = [r for r in rows if r.ship]
        candidates = shipped or rows

    results: list[tuple[str, str]] = []
    for r in candidates:
        tracking_no = re.sub(r"\D", "", str((r.ship or {}).get("invoiceNo") or ""))
        if not r.ship or not TRACKING_PATTERN.fullmatch(tracking_no):
            try:
                _raise_for_unshipped(r, order_no)
            except AdapterError as e:
                e.sent_request = sent_request
                raise
        courier, fetched = courier_name(context, r.ship, order_no)
        sent_request = sent_request or fetched
        results.append((tracking_no, courier))

    if len({tracking_no for tracking_no, _ in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "주문옵션으로 어느 상품인지 고르지 못했습니다. 상품별로 나눠 배송된 것으로 보입니다.")
    tracking_no, courier = results[0]
    return TrackingResult(tracking_no=tracking_no, courier=courier, sent_request=sent_request)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    order, sent_request = _locate_order(context, order_no)
    found = order.order_date or order_date_from_no(order_no)
    return attach_order_date(found, lambda: _lookup(context, order, order_option, sent_request))
