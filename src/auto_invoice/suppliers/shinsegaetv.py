"""신세계TV쇼핑(슥홈쇼핑, www.shinsegaetvshopping.com) 공급사 어댑터.

리버스엔지니어링 결과 (2026-09-08 실측):
- 샵마인 엑셀의 "상품URL"에는 주문별 상세 주소가 들어온다:
    https://www.shinsegaetvshopping.com/orderlist/detail?orderNo=<주문번호>
  그런데 이 **주문상세 화면에는 배송상태도 배송조회 버튼도 없다** (템플릿에
  주석 처리된 채로 남아 있다). 상태와 배송조회는 **주문목록**(/orderlist/list)
  의 상품 행에만 나온다. 그래서 이 어댑터는 상세 대신 목록을 연다.
- 주문번호는 앞 8자리가 주문일(YYYYMMDD)이다 (20260907237962 -> 2026.09.07,
  목록의 주문일과 일치). 목록은 조회기간을 주소로 받으므로
    /orderlist/list?searchMonth=0&fromDate=YYYYMMDD&toDate=YYYYMMDD&currentPage=1&rowsPerPage=10
  처럼 **그 날 하루**로 좁혀 열면 그 주문만 딱 나온다 (실측: 총 1건).
  앞 8자리가 날짜가 아니거나 그 날 목록에 없으면 12개월치(rowsPerPage=100,
  실측 51건이 한 화면에 다 나옴)를 넘겨 가며 찾는다.
- 목록의 주문 하나는 div.boxs-list-dv > dl 이고, dt의 p.order-info 에 주문일
  (strong.date "2026.09.07")과 주문번호(span.no "(주문번호: 2026...)")가,
  dd의 table tbody tr 이 상품 한 줄이다. 줄마다 옵션(div.area-options
  "옵션 : 화이트100/10(280)"), 상태(td.td_state "배송중"/"배송완료"), 그리고
  발송된 줄에만 [배송조회] 버튼 onclick="searchShip('<주문번호>', '<상품순번>', ...)"
  이 있다. 상품순번(orderGseq)은 '001'부터다.
- 배송조회 버튼이 여는 팝업 주소는 /mypage/order/ship/<주문번호>/<상품순번> 이고
  서버가 그려서 내려주므로 화면을 열지 않고 context.request로 받는다. 안에
  <dl><dt>라벨</dt><dd>값</dd></dl> 꼴로 받으실분 / 배송상태 / 송장번호 /
  택배업체("CJ 대한통운")가 있고, 위쪽에 그 줄의 옵션도 다시 나와서 엉뚱한
  줄을 읽지 않았는지 검산할 수 있다.
- 로그인이 안 되어 있으면 /member/login?forwardUrl=... 으로 302된다. 폼은
  input#memId / input#passwd / button#loginButton, POST /member/login-submit.
  아이디+비밀번호를 채우고 버튼을 자동 클릭해도 캡차 없이 통과했다
  (SSG/더현대/NS홈쇼핑/11번가/옥션과 같은 패턴). 사용자가 "쿠키로 첫 로그인부터
  자동"을 요청해서 SHINSEGAETV_ID/SHINSEGAETV_PW로 완전 자동 로그인하고, 세션은
  storage_state(auth/shinsegaetv_state.json)로 저장돼 다음 실행부터는 쿠키만으로
  바로 조회된다 (로그인 쿠키 JSESSIONID/custNo는 세션 쿠키지만 storage_state가
  같이 저장한다).
- 택배사 표기: "CJ 대한통운" -> CJ대한통운, "롯데..." -> 롯데택배, "DELIBOX" ->
  딜리박스는 common.normalize_courier 가 처리한다 (사용자 요청 4·5번).

주문에 상품이 여러 줄이면 (사용자 요청 6번: "주문 옵션을 비교해서 찾아줘"):
  1. 샵마인 엑셀의 "주문옵션"이 어느 줄의 옵션에만 유일하게 들어 있으면 그 줄
  2. 아니면 옥션과 같은 토큰 점수(auction.option_score)로 1등이 유일하면 그 줄
  3. 그래도 못 고르면 발송된 줄을 전부 조회해서 송장이 하나뿐이면 그것, 서로
     다르면 사람이 보도록 ParseError
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from ..models import TrackingResult
from . import common
from .auction import option_score
from .base import (
    BlockedError,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
)

load_dotenv()

DOMAINS = {"shinsegaetvshopping.com", "www.shinsegaetvshopping.com", "m.shinsegaetvshopping.com"}
SITE_KEY = "shinsegaetv"

# 주문당 목록 화면 1개 + 팝업 요청 1개. 11번가/네이버와 같은 간격.
REQUEST_GAP = (1.0, 2.0)

BASE_URL = "https://www.shinsegaetvshopping.com"
LIST_URL = (BASE_URL + "/orderlist/list?searchMonth={month}&fromDate={from_date}"
            "&toDate={to_date}&currentPage={page}&rowsPerPage={rows}")
SHIP_URL = BASE_URL + "/mypage/order/ship/{order_no}/{gseq}"
LOGIN_PATH = "/member/login"

LOGIN_ID_SELECTOR = "#memId"
LOGIN_PW_SELECTOR = "#passwd"
LOGIN_BUTTON_SELECTOR = "#loginButton"
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000

# 날짜로 못 찾았을 때 넘겨 보는 12개월치 목록의 페이지 크기와 최대 페이지 수.
FALLBACK_ROWS_PER_PAGE = 100
FALLBACK_MAX_PAGES = 3

ORDER_NO_PREFIX_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})\d+$")
SHIP_CALL_PATTERN = re.compile(r"searchShip\('(\d+)',\s*'(\d+)'")
OPTION_LABEL_PATTERN = re.compile(r"^옵션\s*:\s*")
TRACKING_PATTERN = re.compile(r"\d{9,}")

# 목록 화면에서 주문 하나(dl)를 찾아 상품 줄을 한 번에 뽑는다. 줄마다 셀렉터를
# 따로 물어보면 왕복이 늘어난다 (옥션 어댑터와 같은 이유).
PARSE_ORDER_JS = """(orderNo) => {
    for (const dl of document.querySelectorAll('.boxs-list-dv dl')) {
        const no = (dl.querySelector('.order-info .no')?.textContent || '').replace(/\\s+/g, '');
        if (!no.includes(orderNo)) continue;
        const rows = [];
        for (const tr of dl.querySelectorAll('tbody tr')) {
            const text = (sel) => (tr.querySelector(sel)?.textContent || '').replace(/\\s+/g, ' ').trim();
            const ship = [...tr.querySelectorAll('button')]
                .map((b) => b.getAttribute('onclick') || '')
                .find((o) => o.includes('searchShip')) || '';
            rows.push({option: text('.area-options'), state: text('.td_state'), ship});
        }
        return {orderDate: (dl.querySelector('.order-info .date')?.textContent || '').trim(), rows};
    }
    return null;
}"""

DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값

RECIPIENT_LABEL = "받으실분"
STATE_LABEL = "배송상태"
TRACKING_LABEL = "송장번호"
COURIER_LABEL = "택배업체"


def extract_order_no(product_url: str) -> str:
    values = parse_qs(urlparse(product_url).query).get("orderNo")
    if not values or not values[0].strip():
        raise ParseError(f"URL에서 orderNo 파라미터를 찾을 수 없습니다: {product_url}")
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
# 로그인
# --------------------------------------------------------------------------

def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(page, lambda url: LOGIN_PATH in url)


def _auto_login(page: Page) -> bool:
    """SHINSEGAETV_ID/SHINSEGAETV_PW로 완전 자동 로그인한다 (사용자 명시 요청)."""
    login_id = os.environ.get("SHINSEGAETV_ID")
    login_pw = os.environ.get("SHINSEGAETV_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "신세계TV쇼핑 로그인이 필요하지만 SHINSEGAETV_ID/SHINSEGAETV_PW 환경변수가 "
            "설정되어 있지 않습니다. .env에 추가해주세요.")
    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    page.click(LOGIN_BUTTON_SELECTOR)
    return common.wait_for_url(page, lambda url: LOGIN_PATH not in url, LOGIN_WAIT_TIMEOUT_MS,
                               poll_ms=500)


def _open_logged_in(page: Page, url: str) -> None:
    common.goto_settled(page, url)
    if not _looks_like_login_page(page):
        return
    common.safe_print("[shinsegaetv] 로그인 세션이 없어 자동 로그인을 시도합니다.")
    if not _auto_login(page):
        raise BlockedError("신세계TV쇼핑 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
    # 로그인 폼의 forwardUrl이 목록으로 돌려보내 주지만, 조회기간 파라미터까지
    # 그대로 오는지에 기대지 않고 원하던 주소를 다시 연다.
    common.goto_settled(page, url)
    if _looks_like_login_page(page):
        raise BlockedError("신세계TV쇼핑 로그인 후에도 여전히 로그인 페이지입니다.")


# --------------------------------------------------------------------------
# 주문목록에서 주문 찾기
# --------------------------------------------------------------------------

def _find_order(page: Page, order_no: str) -> dict | None:
    # 서버가 그려서 내려주는 화면이라 goto 직후 바로 있지만, 혹시 늦게 그려지는
    # 경우를 위해 잠깐만 지켜본다 (없으면 곧장 False로 끝난다).
    common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS)
    return page.evaluate(PARSE_ORDER_JS, order_no)


def _locate_order(page: Page, order_no: str) -> dict:
    """주문번호로 목록을 좁혀 그 주문의 상품 줄들을 얻는다."""
    found_date = order_date_from_no(order_no)
    if found_date is not None:
        ymd = found_date.strftime("%Y%m%d")
        _open_logged_in(page, LIST_URL.format(month=0, from_date=ymd, to_date=ymd, page=1, rows=10))
        order = _find_order(page, order_no)
        if order is not None:
            return order

    for page_no in range(1, FALLBACK_MAX_PAGES + 1):
        _open_logged_in(page, LIST_URL.format(month=12, from_date="", to_date="",
                                              page=page_no, rows=FALLBACK_ROWS_PER_PAGE))
        order = _find_order(page, order_no)
        if order is not None:
            return order
        if page.locator(".boxs-list-dv dl").count() < FALLBACK_ROWS_PER_PAGE:
            break  # 마지막 페이지였다
    raise OrderNotFound(f"주문내역(최근 12개월)에서 주문번호 {order_no}을(를) 찾지 못했습니다.")


def _parse_list_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y.%m.%d").date()
    except ValueError:
        return None


def _strip_option_label(text: str) -> str:
    return OPTION_LABEL_PATTERN.sub("", text or "").strip()


# --------------------------------------------------------------------------
# 상품 줄 고르기 (사용자 요청 6번)
# --------------------------------------------------------------------------

def select_row(rows: list[dict], order_option: str | None) -> dict | None:
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
        contained = [r for r in rows if target in normalize_option(_strip_option_label(r["option"]))]
        if len(contained) == 1:
            return contained[0]
    scored = sorted(((option_score(order_option, _strip_option_label(r["option"])), i)
                     for i, r in enumerate(rows)), reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return rows[scored[0][1]]
    return None


# --------------------------------------------------------------------------
# 배송조회 팝업
# --------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _field_values(html: str) -> dict[str, str]:
    """팝업의 <dl><dt>라벨</dt><dd>값</dd></dl> 쌍."""
    return {_strip_tags(label): _strip_tags(value)
            for label, value in re.findall(r"<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)}


def _fetch_ship(context: BrowserContext, order_no: str, gseq: str, expected_option: str) -> dict:
    """배송조회 팝업을 HTML로 받아 라벨-값 사전으로 돌려준다 (옵션 검산 포함)."""
    response = context.request.get(SHIP_URL.format(order_no=order_no, gseq=gseq))
    if LOGIN_PATH in response.url:
        raise BlockedError(f"배송조회 팝업에서 로그인이 풀렸습니다 (주문번호={order_no}).")
    html = response.text()
    fields = _field_values(html)
    if TRACKING_LABEL not in fields and STATE_LABEL not in fields:
        raise ParseError(f"배송조회 팝업 구조를 해석하지 못했습니다 (주문번호={order_no}, 순번={gseq}).")
    # 팝업 위쪽에 그 줄의 옵션이 다시 나온다 - 엉뚱한 순번을 읽지 않았는지 검산
    popup_option = next((m.group(1) for m in re.finditer(
        r'class="area-options"[^>]*>(.*?)</div>', html, re.S)), "")
    want = normalize_option(_strip_option_label(expected_option))
    got = normalize_option(_strip_option_label(_strip_tags(popup_option)))
    if want and got and want not in got:
        raise ParseError(
            f"배송조회 팝업의 옵션({_strip_tags(popup_option)})이 목록의 옵션({expected_option})과 "
            f"다릅니다 (주문번호={order_no}, 순번={gseq}).")
    return fields


def _tracking_from_fields(fields: dict[str, str], order_no: str, gseq: str) -> tuple[str, str]:
    state = fields.get(STATE_LABEL, "")
    raw_tracking = fields.get(TRACKING_LABEL, "")
    m = TRACKING_PATTERN.search(raw_tracking.replace("-", ""))
    if not m:
        raise_if_cancelled(state, order_no)
        raise TrackingNotAvailableYet(
            f"배송조회 팝업에 아직 송장번호가 없습니다 (주문번호={order_no}, 순번={gseq}, 상태={state}).")
    courier_name = fields.get(COURIER_LABEL, "").strip()
    courier = common.normalize_courier(courier_name) if courier_name else DEFAULT_COURIER
    return m.group(0), courier


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _lookup(context: BrowserContext, order: dict, order_no: str, order_option: str | None) -> TrackingResult:
    rows = order["rows"]
    if not rows:
        raise ParseError(f"주문목록에서 상품 줄을 읽지 못했습니다 (주문번호={order_no}).")

    row = select_row(rows, order_option)
    if row is not None:
        candidates = [row]
    else:
        # 옵션으로 특정할 수 없으면 발송된 줄을 전부 조회해서 송장이 하나뿐인지 본다
        # (다른 어댑터와 같은 안전 규칙). 발송된 줄이 없으면 전부를 상태 판정에 쓴다.
        shipped = [r for r in rows if SHIP_CALL_PATTERN.search(r["ship"])]
        candidates = shipped or rows

    results: list[tuple[str, str]] = []
    for r in candidates:
        m = SHIP_CALL_PATTERN.search(r["ship"])
        if not m:
            state = r["state"]
            raise_if_cancelled(state, order_no)
            raise TrackingNotAvailableYet(
                f"아직 배송조회가 열리지 않은 주문입니다 (주문번호={order_no}, 상태={state or '없음'}).")
        gseq = m.group(2)
        fields = _fetch_ship(context, order_no, gseq, r["option"])
        results.append(_tracking_from_fields(fields, order_no, gseq))

    if len({tracking_no for tracking_no, _ in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "주문옵션으로 어느 상품인지 고르지 못했습니다. 상품별로 나눠 배송된 것으로 보입니다.")
    tracking_no, courier = results[0]
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        order = _locate_order(page, order_no)
        found = _parse_list_date(order.get("orderDate", "")) or order_date_from_no(order_no)
        return attach_order_date(found, lambda: _lookup(context, order, order_no, order_option))
    finally:
        page.close()
