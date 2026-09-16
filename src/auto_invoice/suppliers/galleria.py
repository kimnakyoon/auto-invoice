"""갤러리아몰(www.galleria.co.kr) 공급사 어댑터.

리버스엔지니어링 결과 (2026-09-16 실측):
- 주문목록은 https://www.galleria.co.kr/mypage/initOrderList.action 이고, 실제 표는
  그 안에서 jQuery load로 받아오는 서버 렌더 HTML 조각이다:
    GET /mypage/getOrderList.action?page_idx=<쪽>&date_start=YYYY-MM-DD&date_end=YYYY-MM-DD
  주문상세는 /mypage/initOrderDetail.action?ord_no=<주문번호> (전체 페이지). 둘 다
  **브라우저 화면 없이 context.request로 받는다** - 페이지는 로그인할 때만 잠깐 연다.
- 주문번호 앞 8자리가 주문일(YYYYMMDD)이다 (202609158340465 -> 2026-09-15, 6개월치
  전부 목록의 주문일과 일치). 목록은 주문 5건씩이고 아래 paging 블록의
  <a class="num" data-value="N"> 으로 쪽수를 안다. 조회기간은 최대 6개월(화면 JS가 막는다).
- 목록·상세 모두 주문의 상품 한 줄이 <tr name="goods_info" data-goods-no="…"> 이고
    - span.opt            "베이지 E2/265" (주문옵션)
    - span.state          진행상태. 실측: 상품준비중 / 취소완료 / 구매확정. 화면 안내문의
                          단계는 결제대기 → 결제완료 → 상품준비중 → 물류센터이동중/배송대기 →
                          배송중 → 배송완료 (→ 구매확정)
    - span.cdt            "발송예정일 2026-09-20" / "배송완료일 2026-08-06" 같은 날짜 안내
    - [배송조회] 버튼     발송된 줄에만 있고 onclick="overpass.myspopup.deliInfoLayer({ord_no:'…',
                          ord_dtl_no:'D…', ord_pkg_seq:'0000'})" 에 상세번호가 들어 있다
  목록에서는 주문마다 <div class="odr_top"> (주문일, ord_no="…") 헤더가 먼저 오고 그 뒤에
  상품 줄이 온다.
- 송장번호는 [배송조회] 레이어가 부르는 JSON에서 읽는다:
    POST /mypage/searchSetDeliInfo.action  form: ord_no, ord_dtl_no
    -> {"deliInfo": "<JSON 문자열>", "invoiceInfoList": "<JSON 문자열>"}
    deliInfo: invoice_no "316977324333", parcel_comp_cd "롯데택배"(택배사 **이름**이 그대로),
              recvr_nm "무한타올"(가려지지 않은 수령인), msk_recvr_nm "무**올"
  택배사 이름은 common.normalize_courier를 거쳐 CJ/대한통운 -> CJ대한통운, 롯데 -> 롯데택배,
  DELIBOX -> 딜리박스로 맞춘다 (사용자 요청 4·5번).
- 로그인이 안 되어 있으면 목록/상세 요청이 302가 아니라 **200에 3KB짜리 껍데기**로 오고
  그 안에 overpass.link('LOGINPAGE') 한 줄이 있다(자바스크립트로 /login/login.action에
  보낸다). 이 문자열로 판정한다. 없는 주문번호로 상세를 열면 "시스템 장애 안내"(500 안내
  페이지, HTTP 200)가 온다.
- 로그인 화면(/login/login.action)은 두 단계다: #login_id를 채우고 [다음](#next_btn)을
  누르면 POST /login/getCertKey.action 으로 login_cert_key를 받아 숨은 칸에 넣고 비밀번호
  칸이 펼쳐진다. #pwd를 채우고 [로그인](#lgn_btn)을 누르면 POST /login/generalLogin.action
  (login_cert_key, login_id, pwd, sCaptcha, answer, …) 이 JSON으로 답한다:
    성공: {"req_code":"S","rstMessage":{"memType":"O", ...}}  -> SSO 토큰 iframe을 거쳐
          returnUrl(없으면 메인)로 location.replace
    실패: memType "N"(회원정보 불일치) / "S"(SNS 가입 계정) / sCaptcha "Y"(5회 실패 뒤
          보안문자 요구) 등. 화면에는 "입력정보와 일치하는 회원 정보가 없습니다"로 뜬다.
  reCAPTCHA/Turnstile 없이 **번들 크로미엄 headless로 첫 시도에 통과했다**. 로그인 화면은
  광고·분석 태그를 수십 개 불러서, 그 페이지에만 route를 걸어 galleria.co.kr 밖은 끊는다.
  사용자가 "쿠키로 첫 로그인부터 자동"을 요청해서 GALLERIA_ID/GALLERIA_PW로 완전 자동
  로그인하고, 세션은 storage_state(auth/galleria_state.json)로 저장돼 다음 실행부터는
  쿠키만으로 바로 조회된다. 로그인 세션(SESSION 쿠키)은 브라우저 세션 쿠키라 서버가
  끊으면 다시 로그인한다.

샵마인 엑셀의 "상품URL"은 실측(2026-09-16 실행)으로 /mypage/initOrderDetail.action?ord_no=…
꼴이지만, 어떤 꼴이든 세 가지로 받는다:
  1. ord_no=<주문번호> 가 있으면(주문상세 주소) 그 상세를 바로 연다. 이번에 조회할 주문이
     2건 이상이면 prepare_batch가 가장 오래된 주문일부터 오늘까지의 목록을 한 번 받아 캐시해
     두고(상세 95KB/건 대신 목록 27KB/5건), 캐시로 답한 주문은 요청을 안 보낸 것으로
     표시해 오케스트레이터가 간격을 두지 않는다(sent_request=False)
  2. goods_no=<상품번호> 가 있으면(상품 상세 주소 /goods/initDetailGoods.action?goods_no=…)
     주문목록에서 그 상품 줄만 후보로 본다
  3. 둘 다 없으면(주문목록 주소 등) 주문목록 전체가 후보다
2·3은 옥션과 같은 방식으로 **주문옵션 + 수령인**으로 어느 주문인지 고른다 (사용자 요청
6번 "찾기 힘들 때는 주문 옵션을 비교"). 옵션이 같은 주문이 여러 건(같은 상품을 여러
고객에게)인 것이 보통이라 수령인까지 맞아야 확정한다 - 발송된 줄은 배송정보 JSON의
recvr_nm(가려지지 않음)으로, 아직 안 나간 줄은 주문상세의 받는사람("장*옥"처럼 가운데가
가려짐)으로 비교한다. 최근 1개월에서 못 찾으면 6개월로 넓혀 다시 본다 (사용자 요청 7번).
한 주문에 상품이 여러 줄이면 이랜드몰과 같은 규칙으로 옵션이 유일하게 맞는 줄을 고른다.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

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
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
)

load_dotenv()

DOMAINS = {"galleria.co.kr", "www.galleria.co.kr", "m.galleria.co.kr"}
SITE_KEY = "galleria"

# 상품URL만으로 주문을 특정할 수 없을 때 수령인까지 본다 (옥션과 같은 표시 -
# orchestrator가 get_tracking에 recipient_name을 넘겨준다).
WANTS_RECIPIENT_NAME = True

# 화면 없이 가벼운 HTML/JSON 요청만 보내고 봇 확인도 없는 사이트 - 이랜드몰과 같은 간격.
REQUEST_GAP = (0.5, 1.2)

BASE_URL = "https://www.galleria.co.kr"
LIST_URL = BASE_URL + "/mypage/getOrderList.action?page_idx={page}&date_start={from_date}&date_end={to_date}"
DETAIL_URL = BASE_URL + "/mypage/initOrderDetail.action?ord_no={order_no}"
DELI_INFO_URL = BASE_URL + "/mypage/searchSetDeliInfo.action"
LOGIN_URL = BASE_URL + "/login/login.action"
LOGIN_PATH = "/login/login"
# 로그인이 없을 때 서버가 200으로 내려주는 껍데기 페이지의 표식.
LOGIN_REDIRECT_MARKER = "overpass.link('LOGINPAGE')"
# 없는 주문번호로 상세를 열면 오는 안내 페이지의 제목.
ERROR_PAGE_MARKER = "시스템 장애 안내"

LOGIN_ID_SELECTOR = "#login_id"
LOGIN_NEXT_SELECTOR = "#next_btn"
LOGIN_PW_SELECTOR = "#pwd"
LOGIN_BUTTON_SELECTOR = "#lgn_btn"
CERT_KEY_API_PATH = "/login/getCertKey"
LOGIN_API_PATH = "/login/generalLogin"
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000
LOGIN_ALLOWED_HOSTS = ("galleria.co.kr",)

# 목록은 주문 5건씩. 사이트가 허용하는 조회기간 상한(6개월)보다 조금 안쪽으로 잡는다.
LIST_MAX_PAGES = 40
LIST_SHORT_DAYS = 31          # 먼저 보는 기간 (최근 1개월)
LIST_MAX_DAYS = 6 * 30 - 1    # 못 찾으면 여기까지 넓힌다 (6개월)
# 수령인을 확인하려고 주문상세/배송정보를 여는 후보 수 상한 (옥션과 같은 생각 -
# 옵션이 조금이라도 겹치는 주문 전부를 열면 끝이 없다).
MAX_RECIPIENT_LOOKUPS = 8

ORDER_NO_PREFIX_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})\d+$")
ORDER_HEADER_PATTERN = re.compile(r'<div class="odr_top">(.*?)</div>', re.S)
# 목록 조각에서는 따옴표 없이 ord_no=2026… 로 온다 (전체 페이지에서는 따옴표가 있다).
HEADER_ORDER_NO_PATTERN = re.compile(r"""ord_no=["']?(\d+)""")
HEADER_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
ROW_PATTERN = re.compile(r'<tr name="goods_info"[^>]*>.*?</tr>', re.S)
GOODS_NO_PATTERN = re.compile(r'data-goods-no="([^"]*)"')
OPTION_PATTERN = re.compile(r'class="opt">(.*?)</span>', re.S)
STATE_PATTERN = re.compile(r'class="state">(.*?)</span>', re.S)
DATE_NOTE_PATTERN = re.compile(r'class="cdt">(.*?)</span>', re.S)
DELI_PARAMS_PATTERN = re.compile(
    r"deliInfoLayer\(\{\s*ord_no:'([^']*)',\s*ord_dtl_no:'([^']*)',\s*ord_pkg_seq:'([^']*)'")
PAGING_PATTERN = re.compile(r'<div class="paging">(.*?)</div>', re.S)
# 쪽 번호 링크(class="num")와, 눌리는 상태의 [다음](10쪽 묶음을 넘길 때 다음 묶음의 첫 쪽).
# 마지막 쪽의 [다음]은 disabled="true"라 빼야 한다 - 안 빼면 없는 쪽을 한 번 더 받는다.
PAGE_LINK_PATTERN = re.compile(r"<a\b([^>]*)>")
PAGE_VALUE_PATTERN = re.compile(r'data-value="(\d+)"')
RECIPIENT_PATTERN = re.compile(r'name="deli_recvr"[^>]*>(.*?)</span>', re.S)
TRACKING_PATTERN = re.compile(r"\d{9,}")

NOT_YET_PATTERNS = ("결제대기", "결제완료", "상품준비중", "물류센터", "배송대기", "주문접수")
# 반품이 끝난 줄은 송장이 있어도 다시 보낼 것이 없고, 없으면 영영 안 나온다.
RETURNED_KEYWORD = "반품"
DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값


@dataclass
class OrderRow:
    goods_no: str
    option: str          # "베이지 E2/265"
    state: str           # "상품준비중" / "구매확정" / "취소완료" / ...
    date_note: str       # "발송예정일 2026-09-20" 같은 안내 (없으면 "")
    detail_no: str | None = None   # [배송조회]의 ord_dtl_no. 없으면 아직 발송 전

    @property
    def shipped(self) -> bool:
        return bool(self.detail_no)


@dataclass
class ListedOrder:
    order_no: str
    order_date: date | None
    rows: list[OrderRow] = field(default_factory=list)


@dataclass
class DeliInfo:
    tracking_no: str
    courier: str
    recipient: str


# 컨텍스트(=이번 실행의 브라우저)별로 읽어둔 주문목록. 키는 (컨텍스트, 기간 일수).
_listed_orders: dict[tuple[int, int], list[ListedOrder]] = {}
# prepare_batch가 주문번호별로 읽어둔 목록 (주문번호 URL 경로용). 캐시에 있으면 주문상세를
# 열지 않는다 - 상세는 한 건에 95KB인데 목록은 5건에 27KB다.
_listed_by_no: dict[int, dict[str, ListedOrder]] = {}
# 1건이면 목록이나 상세나 요청 하나라 이득이 없다.
LIST_PREFETCH_MIN_ORDERS = 2
# 주문상세에서 읽은 (주문번호 -> 가려진 수령인) - 같은 실행에서 두 번 열지 않는다.
_recipients: dict[tuple[int, str], str] = {}


# --------------------------------------------------------------------------
# 상품URL 해석
# --------------------------------------------------------------------------

def _query_param(product_url: str, name: str) -> str | None:
    values = parse_qs(urlparse(product_url).query).get(name)
    value = (values[0] if values else "").strip()
    return value or None


def extract_order_no(product_url: str) -> str | None:
    """상품URL에 주문번호(ord_no)가 있으면 꺼낸다. 없으면 None - 목록에서 찾는다."""
    return _query_param(product_url, "ord_no")


def extract_goods_no(product_url: str) -> str | None:
    return _query_param(product_url, "goods_no")


def order_date_from_no(order_no: str) -> date | None:
    """주문번호 앞 8자리가 날짜면 그 날짜 (아니면 None)."""
    m = ORDER_NO_PREFIX_DATE.match(order_no or "")
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


def _needs_login(html: str) -> bool:
    return LOGIN_REDIRECT_MARKER in html


def _login_failure_reason(body: dict) -> str | None:
    """generalLogin 응답에서 실패 사유 (성공이면 None). 화면 JS의 분기를 그대로 옮겼다."""
    if not body:
        return None
    result = body.get("rstMessage") or {}
    mem_type = str(result.get("memType") or "")
    if str(body.get("req_code")) == "S" and mem_type == "O":
        if str(result.get("forcePwdChngYn")) == "Y" or str(result.get("pwdInitYn")) == "Y":
            return "비밀번호 변경을 요구받았습니다"
        return None
    if str(result.get("sCaptcha")) == "Y":
        return "로그인 실패가 쌓여 보안문자(캡차)를 요구받았습니다"
    if mem_type == "N":
        return "입력정보와 일치하는 회원 정보가 없습니다"
    if mem_type == "S":
        return "SNS로 가입한 계정이라 아이디/비밀번호 로그인이 안 됩니다"
    if mem_type == "P":
        return "회원 보호(휴면/보호) 절차를 요구받았습니다"
    return f"req_code={body.get('req_code')}, memType={mem_type or '없음'}"


def _auto_login(page: Page) -> None:
    """GALLERIA_ID/GALLERIA_PW로 완전 자동 로그인한다 (사용자 명시 요청)."""
    login_id = os.environ.get("GALLERIA_ID")
    login_pw = os.environ.get("GALLERIA_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "갤러리아몰 로그인이 필요하지만 GALLERIA_ID/GALLERIA_PW 환경변수가 "
            "설정되어 있지 않습니다. .env에 추가해주세요.")

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    page.wait_for_selector(LOGIN_ID_SELECTOR, state="visible", timeout=LOGIN_WAIT_TIMEOUT_MS)
    page.fill(LOGIN_ID_SELECTOR, login_id)
    # [다음]은 인증키(login_cert_key)를 받아 숨은 칸에 넣고 비밀번호 칸을 펼친다.
    with page.expect_response(lambda r: CERT_KEY_API_PATH in r.url, timeout=LOGIN_WAIT_TIMEOUT_MS):
        page.click(LOGIN_NEXT_SELECTOR)
    page.wait_for_selector(LOGIN_PW_SELECTOR, state="visible", timeout=LOGIN_WAIT_TIMEOUT_MS)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    with page.expect_response(lambda r: LOGIN_API_PATH in r.url,
                              timeout=LOGIN_WAIT_TIMEOUT_MS) as login_response:
        page.click(LOGIN_BUTTON_SELECTOR)
    try:
        body = login_response.value.json()
    except Exception:  # noqa: BLE001 - JSON이 아니면 아래에서 주소로 판정한다
        body = {}
    reason = _login_failure_reason(body)
    if reason:
        raise BlockedError(f"갤러리아몰이 로그인을 거부했습니다: {reason}")
    left = common.wait_for_url(page, lambda url: not _is_login_url(url), LOGIN_WAIT_TIMEOUT_MS,
                               poll_ms=200)
    if not left:
        hint = f" 사이트 안내: {alerts[-1]}" if alerts else ""
        raise BlockedError(f"갤러리아몰 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.{hint}")


def _abort_third_party(route) -> None:
    host = urlparse(route.request.url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in LOGIN_ALLOWED_HOSTS):
        route.continue_()
    else:
        route.abort()


def _login_with_page(context: BrowserContext) -> None:
    """페이지를 잠깐 열어 자동 로그인한다 - 세션이 없을 때만 오는 느린 경로."""
    common.safe_print("[galleria] 로그인 세션이 없어 자동 로그인을 시도합니다.")
    page = context.new_page()
    try:
        # 이 페이지에만 건다 - 조회용 컨텍스트의 공용 라우팅(이미지 차단)은 그대로다.
        page.route("**/*", _abort_third_party)
        common.goto_settled(page, LOGIN_URL)
        if not _is_login_url(page.url):
            return  # 요청 시점과 달리 지금은 로그인이 살아 있다
        _auto_login(page)
    finally:
        page.close()


def _get_html(context: BrowserContext, url: str) -> str:
    """로그인된 상태로 HTML을 받는다. 세션이 없으면 한 번 로그인하고 다시 받는다."""
    html = context.request.get(url).text()
    if not _needs_login(html):
        return html
    _login_with_page(context)
    html = context.request.get(url).text()
    if _needs_login(html):
        raise BlockedError("갤러리아몰 로그인 후에도 여전히 로그인 페이지입니다.")
    return html


# --------------------------------------------------------------------------
# HTML 해석
# --------------------------------------------------------------------------

def _strip_tags(fragment: str) -> str:
    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def _parse_date(text: str) -> date | None:
    m = HEADER_DATE_PATTERN.search(text or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_rows(html: str) -> list[OrderRow]:
    """상품 줄(<tr name="goods_info">)들 - 목록 조각과 주문상세 둘 다 같은 꼴이다."""
    rows: list[OrderRow] = []
    for row_html in ROW_PATTERN.findall(html):
        goods = GOODS_NO_PATTERN.search(row_html)
        option = OPTION_PATTERN.search(row_html)
        state = STATE_PATTERN.search(row_html)
        note = DATE_NOTE_PATTERN.search(row_html)
        deli = DELI_PARAMS_PATTERN.search(row_html)
        rows.append(OrderRow(
            goods_no=goods.group(1).strip() if goods else "",
            option=_strip_tags(option.group(1)) if option else "",
            state=_strip_tags(state.group(1)) if state else "",
            date_note=_strip_tags(note.group(1)) if note else "",
            detail_no=(deli.group(2).strip() or None) if deli else None))
    return rows


def parse_orders(html: str) -> list[ListedOrder]:
    """주문목록 조각에서 주문(odr_top 헤더)마다 뒤따르는 상품 줄을 묶는다."""
    orders: list[ListedOrder] = []
    headers = list(ORDER_HEADER_PATTERN.finditer(html))
    for idx, header in enumerate(headers):
        no_match = HEADER_ORDER_NO_PATTERN.search(header.group(1))
        if not no_match:
            continue
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(html)
        orders.append(ListedOrder(
            order_no=no_match.group(1),
            order_date=_parse_date(_strip_tags(header.group(1))),
            rows=parse_rows(html[header.end():end])))
    return orders


def _last_page(html: str) -> int:
    m = PAGING_PATTERN.search(html)
    if not m:
        return 1
    values = [1]
    for attrs in PAGE_LINK_PATTERN.findall(m.group(1)):
        value = PAGE_VALUE_PATTERN.search(attrs)
        if not value or "disabled" in attrs:
            continue
        if 'class="num' in attrs or 'class="next"' in attrs:
            values.append(int(value.group(1)))
    return max(values)


def _fetch_list(context: BrowserContext, from_date: date, to_date: date) -> list[ListedOrder]:
    """기간 안의 목록을 마지막 쪽까지 받아 주문 순서(최근 것부터) 그대로 모은다."""
    found: list[ListedOrder] = []
    seen: set[str] = set()
    page_no, last = 1, 1
    while page_no <= min(last, LIST_MAX_PAGES):
        html = _get_html(context, LIST_URL.format(
            page=page_no, from_date=from_date.isoformat(), to_date=to_date.isoformat()))
        last = max(last, _last_page(html))
        orders = parse_orders(html)
        if not orders:
            break
        for order in orders:
            if order.order_no not in seen:
                seen.add(order.order_no)
                found.append(order)
        page_no += 1
    return found


def _load_list(context: BrowserContext, days: int) -> list[ListedOrder]:
    """오늘부터 days일 전까지의 목록 (실행 중 한 번만 받아 캐시)."""
    key = (id(context), days)
    if key not in _listed_orders:
        today = date.today()
        _listed_orders[key] = _fetch_list(context, today - timedelta(days=days), today)
        common.safe_print(f"[galleria] 최근 {days}일 주문목록 {len(_listed_orders[key])}건을 읽었습니다.")
    return _listed_orders[key]


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문번호가 2건 이상이면 가장 오래된 주문일부터 오늘까지의 목록을
    한 번 받아 캐시한다 (이랜드몰과 같은 방식).

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 실패하면 아무것도
    읽지 않은 것과 같아서 모든 주문이 주문상세 경로로 간다 - 그래서 어떤 예외도
    밖으로 내보내지 않는다. 캐시로 답한 주문은 요청을 안 보냈으므로(sent_request=False)
    오케스트레이터가 간격도 두지 않는다.
    """
    wanted = {no for no in (extract_order_no(o.product_url) for o in orders) if no}
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return
    dates = [d for d in (order_date_from_no(no) for no in wanted) if d is not None]
    today = date.today()
    floor = today - timedelta(days=LIST_MAX_DAYS)
    # 주문번호에서 날짜를 못 읽은 주문이 하나라도 있으면 상한까지 다 받는다.
    oldest = min(dates) if dates and len(dates) == len(wanted) else floor
    try:
        listed = _fetch_list(context, max(oldest, floor), today)
    except Exception as e:  # noqa: BLE001 - 미리 읽기는 실패해도 주문별 경로가 있다
        common.safe_print(f"[galleria] 주문목록 미리 읽기 실패 - 주문별로 조회합니다: {e}")
        return
    _listed_by_no[id(context)] = {o.order_no: o for o in listed}
    hit = len(wanted & set(_listed_by_no[id(context)]))
    common.safe_print(f"[galleria] 주문목록 {len(listed)}건을 미리 읽었습니다 "
                      f"- 조회 대상 {len(wanted)}건 중 {hit}건이 목록에 있습니다.")


def _fetch_detail(context: BrowserContext, order_no: str) -> str:
    html = _get_html(context, DETAIL_URL.format(order_no=order_no))
    if ERROR_PAGE_MARKER in html and not ROW_PATTERN.search(html):
        raise OrderNotFound(f"갤러리아몰에서 주문번호 {order_no}을(를) 열 수 없습니다 (없는 주문이거나 다른 계정의 주문).")
    return html


def _detail_recipient(context: BrowserContext, order_no: str) -> str:
    """주문상세의 받는사람 ("장*옥"처럼 가운데가 가려져 있다)."""
    key = (id(context), order_no)
    if key not in _recipients:
        m = RECIPIENT_PATTERN.search(_fetch_detail(context, order_no))
        _recipients[key] = _strip_tags(m.group(1)) if m else ""
    return _recipients[key]


def fetch_deli_info(context: BrowserContext, order_no: str, detail_no: str) -> DeliInfo:
    """[배송조회] 레이어가 부르는 JSON에서 송장번호·택배사·수령인을 읽는다."""
    response = context.request.post(
        DELI_INFO_URL, form={"ord_no": order_no, "ord_dtl_no": detail_no},
        headers={"X-Requested-With": "XMLHttpRequest"})
    try:
        outer = response.json()
        info = outer.get("deliInfo")
        info = json.loads(info) if isinstance(info, str) else (info or {})
    except Exception as e:  # noqa: BLE001 - 무엇이 왔든 해석 실패로 묶는다
        raise ParseError(f"배송정보 응답을 해석하지 못했습니다 (주문번호={order_no}, 상세번호={detail_no}): {e}")
    tracking_no = re.sub(r"\D", "", str(info.get("invoice_no") or ""))
    if not TRACKING_PATTERN.fullmatch(tracking_no):
        raise ParseError(f"배송정보에 송장번호가 없습니다 (주문번호={order_no}, 상세번호={detail_no}, 값={info.get('invoice_no')!r}).")
    courier = str(info.get("parcel_comp_cd") or "").strip()
    return DeliInfo(
        tracking_no=tracking_no,
        courier=common.normalize_courier(courier) if courier else DEFAULT_COURIER,
        recipient=str(info.get("recvr_nm") or "").strip())


# --------------------------------------------------------------------------
# 상품 줄 / 주문 고르기 (사용자 요청 6번)
# --------------------------------------------------------------------------

def select_row(rows: list[OrderRow], order_option: str | None) -> OrderRow | None:
    """샵마인 "주문옵션"으로 상품 줄 하나를 고른다. 못 고르면 None (이랜드몰과 같은 규칙)."""
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


def _row_is_dead(row: OrderRow) -> bool:
    """취소/반품된 줄 - 후보에서 뺀다."""
    return any(k in row.state for k in ("취소", RETURNED_KEYWORD))


def _candidates(orders: list[ListedOrder], goods_no: str | None,
                order_option: str | None) -> list[tuple[int, ListedOrder, OrderRow]]:
    """옵션 점수가 0보다 큰 (주문, 줄)들을 점수 높은 순, 같은 점수면 최근 주문부터."""
    found: list[tuple[int, int, ListedOrder, OrderRow]] = []
    for position, order in enumerate(orders):
        for row in order.rows:
            if goods_no and row.goods_no != goods_no:
                continue
            if _row_is_dead(row):
                continue
            score = option_score(order_option, row.option) if order_option else 0
            if goods_no and not order_option:
                score = 1  # 상품번호만으로 걸렀으면 전부 동점 후보다
            if score > 0:
                found.append((score, position, order, row))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [(score, order, row) for score, _, order, row in found]


def _recipient_of(context: BrowserContext, order: ListedOrder, row: OrderRow) -> tuple[str, DeliInfo | None]:
    """후보 줄의 수령인. 발송된 줄은 배송정보(가려지지 않은 이름, 송장도 같이),
    아니면 주문상세(가려진 이름)에서 읽는다."""
    if row.shipped:
        info = fetch_deli_info(context, order.order_no, row.detail_no)
        if info.recipient:
            return info.recipient, info
        return _detail_recipient(context, order.order_no), info
    return _detail_recipient(context, order.order_no), None


def _find_in_list(context: BrowserContext, goods_no: str | None, order_option: str | None,
                  recipient_name: str | None) -> tuple[ListedOrder, OrderRow, DeliInfo | None]:
    """주문목록에서 (상품번호·)주문옵션(+수령인)으로 어느 주문의 어느 줄인지 찾는다.

    최근 1개월에서 먼저 찾고, 후보가 하나도 없으면 6개월로 넓힌다 (사용자 요청 7번).
    """
    if not order_option and not goods_no:
        raise ParseError(
            "갤러리아몰 상품URL에 주문번호가 없어 주문옵션으로 찾아야 하는데 주문옵션이 비어 "
            "있습니다. 샵마인 내보내기에 '주문옵션'/'수령인' 컬럼을 포함해주세요.")

    candidates: list[tuple[int, ListedOrder, OrderRow]] = []
    for days in (LIST_SHORT_DAYS, LIST_MAX_DAYS):
        orders = _load_list(context, days)
        candidates = _candidates(orders, goods_no, order_option)
        if candidates:
            break
    if not candidates:
        raise OrderNotFound(
            f"주문옵션 {order_option!r}"
            + (f"(상품번호 {goods_no})" if goods_no else "")
            + f"과 맞는 주문을 최근 6개월 주문내역({len(orders)}건)에서 찾지 못했습니다.")

    if recipient_name:
        # 옵션이 같은 주문이 여러 고객에게 있는 것이 보통이라, 점수 높은 순서대로
        # 수령인이 같은 주문을 고른다 (옥션과 같은 규칙).
        checked = 0
        for _, order, row in candidates[:MAX_RECIPIENT_LOOKUPS]:
            checked += 1
            name, info = _recipient_of(context, order, row)
            if recipient_matches(name, recipient_name):
                return order, row, info
        raise ParseError(
            f"주문옵션 {order_option!r}으로 찾은 후보 {checked}건 중 수령인이 {recipient_name!r}인 "
            "주문이 없습니다 - 갤러리아몰에서 직접 확인해주세요.")

    best = candidates[0][0]
    tied = [(order, row) for score, order, row in candidates if score == best]
    if len(tied) > 1:
        raise ParseError(
            f"주문옵션 {order_option!r}에 똑같이 들어맞는 주문이 {len(tied)}건이라 어느 주문인지 확정할 수 없습니다 "
            f"(주문번호: {', '.join(o.order_no for o, _ in tied[:5])}). "
            "샵마인 내보내기에 '수령인' 컬럼을 포함하면 자동으로 구분됩니다.")
    order, row = tied[0]
    return order, row, None


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _raise_for_unshipped(row: OrderRow, order_no: str) -> None:
    """송장이 없는 줄의 결론. 취소/반품이면 OrderCancelled, 아니면 미발급."""
    raise_if_cancelled(row.state, order_no)
    if RETURNED_KEYWORD in row.state:
        raise OrderCancelled(
            f"주문 화면에 '{row.state}' 표시가 있습니다 (주문번호={order_no}) - 반품된 주문인지 확인해주세요.")
    if any(p in row.state for p in NOT_YET_PATTERNS) or not row.state:
        raise TrackingNotAvailableYet(
            f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={row.state or '없음'}).")
    raise ParseError(
        f"배송조회가 없는데 진행상태를 알 수 없습니다 (주문번호={order_no}, 상태={row.state}).")


def _result_for_row(context: BrowserContext, order_no: str, row: OrderRow,
                    info: DeliInfo | None, *, sent_request: bool = True) -> TrackingResult:
    """sent_request: 이 줄을 얻기까지 요청을 보냈는가 (prepare_batch 캐시면 False).
    발송된 줄은 어차피 배송정보 요청을 보내므로 결과는 늘 True다."""
    if not row.shipped:
        try:
            _raise_for_unshipped(row, order_no)
        except AdapterError as e:
            e.sent_request = sent_request
            raise
    info = info or fetch_deli_info(context, order_no, row.detail_no)
    return TrackingResult(tracking_no=info.tracking_no, courier=info.courier,
                          delivery_note=eta_mod.from_text(row.date_note) or (row.date_note or None))


def _lookup_by_order_no(context: BrowserContext, order_no: str,
                        order_option: str | None) -> TrackingResult:
    cached = _listed_by_no.get(id(context), {}).get(order_no)
    if cached is not None:
        rows, sent_request = cached.rows, False
    else:
        rows, sent_request = parse_rows(_fetch_detail(context, order_no)), True
    if not rows:
        raise ParseError(f"주문상세에서 상품 줄을 읽지 못했습니다 (주문번호={order_no}).")
    row = select_row(rows, order_option)
    if row is not None:
        return _result_for_row(context, order_no, row, None, sent_request=sent_request)

    # 옵션으로 특정할 수 없으면 발송된 줄을 전부 보고 송장이 하나뿐인지 본다
    # (다른 어댑터와 같은 안전 규칙). 발송된 줄이 없으면 전부를 상태 판정에 쓴다.
    shipped = [r for r in rows if r.shipped]
    if not shipped:
        _result_for_row(context, order_no, rows[0], None, sent_request=sent_request)
    results = [_result_for_row(context, order_no, r, None) for r in shipped]
    if len({r.tracking_no for r in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "주문옵션으로 어느 상품인지 고르지 못했습니다. 상품별로 나눠 배송된 것으로 보입니다.")
    return results[0]


def get_tracking(
    context: BrowserContext,
    product_url: str,
    headless: bool = True,
    order_option: str | None = None,
    recipient_name: str | None = None,
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    if order_no:
        return attach_order_date(order_date_from_no(order_no),
                                 lambda: _lookup_by_order_no(context, order_no, order_option))

    order, row, info = _find_in_list(context, extract_goods_no(product_url), order_option, recipient_name)
    found = order.order_date or order_date_from_no(order.order_no)
    return attach_order_date(found, lambda: _result_for_row(context, order.order_no, row, info))
