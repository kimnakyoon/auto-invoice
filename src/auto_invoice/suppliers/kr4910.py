"""4910(4910.kr) 공급사 어댑터.

리버스엔지니어링 결과(2026-09-02 실측):
- 4910은 에이블리(㈜에이블리코퍼레이션)가 운영하는 남성 패션몰이라 백엔드가
  에이블리 API(api.a-bly.com)다. 반드시 `x-app-type: AGLO` 헤더를 줘야 한다 -
  없으면 같은 계정의 **에이블리 앱 주문**이 나온다(실측: 14건이 2건으로 바뀜).
- 인증은 로그인 때 발급되는 JWT(쿠키 ably-jwt-token, 만료 약 1년)를
  `authorization: JWT <토큰>` 헤더로 실어 보내는 방식이다. 쿠키가
  storage_state(auth/4910_state.json)에 저장되므로 최초 1회만 로그인하면
  다음 실행부터는 로그인 없이 API 조회만 한다.
- 로그인: https://4910.kr/login/email 폼(input[name=email]/[name=password])을
  headless 번들 크로미엄으로 채워 제출하면 통과한다(실측 - 캡차 없음).
  로그인 API(POST /aglo/api/login/)를 직접 부르는 것은 안 된다 - 로그인
  화면의 자바스크립트가 발급받는 x-anonymous-token 헤더가 없으면 401이다.
- 조회는 페이지를 열지 않고 API 두 개로 한다:
  1) 주문목록 GET /aglo/api/orders/?page=N (10건씩, 최신순) - 주문번호(sno),
     주문상품번호(order_items[].sno), 옵션(options=["블랙","XL"]), 상태,
     주문일(ordered_at), 발송예정 문구까지 통째로 온다. 컨텍스트당 한 번만
     읽어두고(캐시) 모든 주문에 재사용한다.
  2) 발송된 상품만 GET /webview/order_items/<주문상품번호>/delivery-tracking/
     - delivery_info.invoice(송장번호)와 delivery_info.delivery.name
     ("CJ대한통운")이 온다. 주문상세(/aglo/api/orders/<sno>/)에는 송장이
     **없다**(delivery_info: null) - 반드시 이 API를 불러야 한다.
- 상태는 order_items[].processing_status 코드다 (실측):
  1=결제완료(미발급), 3=배송중, 4=배송완료, 5=구매확정. 반품/환불은
  processing_sub_status=44 + delivery_information_message("환불 완료 ...")로
  온다. 반품 건에도 delivery_post_sno(원래 나간 송장)가 남아 있으므로 취소
  판정을 송장 조회보다 먼저 한다.
- 발송 전 주문은 delivery_post_sno가 null이다 - 이걸로 미발급을 가른다.
- 샵마인 엑셀의 상품URL 형태를 아직 실측하지 못해 세 가지를 다 받는다:
  · https://4910.kr/order/<주문번호>  -> 주문번호로 바로 특정
  · https://4910.kr/goods/<상품번호> (desktop/goods 포함) -> 주문목록에서
    그 상품이 든 주문을 찾는다
  · 그 외 4910.kr 주소 -> 주문목록에서 "주문옵션"으로 찾는다
  상품/옵션이 같은 주문이 여러 개면(같은 상품을 여러 고객에게 사주는 경우)
  주문옵션 -> 수령인 이름(주문상세의 receiver_name) 순서로 좁힌다. 그래도
  하나로 특정하지 못하면 송장번호가 전부 같을 때만 쓰고, 다르면 사람이
  확인하도록 예외를 던진다.
- 택배사는 한글 정식 명칭("CJ대한통운")으로 오지만 표기가 흔들려도(CJ/
  대한통운/롯데/DELIBOX 등) 샵마인이 아는 이름(CJ대한통운/롯데택배/딜리박스)
  이 되도록 항상 common.normalize_courier를 태운다.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    AdapterError,
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
)

load_dotenv()

DOMAINS = {"4910.kr", "www.4910.kr", "m.4910.kr"}
SITE_KEY = "4910"

# 옥션처럼 상품URL만으로 주문을 특정하지 못할 수 있어 수령인 이름도 받는다.
WANTS_RECIPIENT_NAME = True

API_HOST = "https://api.a-bly.com"
LIST_URL = API_HOST + "/aglo/api/orders/?page={page}"
DETAIL_URL = API_HOST + "/aglo/api/orders/{order_no}/"
TRACKING_URL = API_HOST + "/webview/order_items/{item_sno}/delivery-tracking/"

LOGIN_URL = "https://4910.kr/login/email"
LOGIN_EMAIL_SELECTOR = "input[name='email']"
LOGIN_PW_SELECTOR = "input[type='password']"

JWT_COOKIE = "ably-jwt-token"

LIST_MAX_PAGES = 10  # 10건씩 -> 최대 100건. 그보다 오래된 주문은 상세 폴백으로.
# 수령인으로 주문을 좁힐 때 열어볼 주문상세 개수 상한 - 후보가 이보다 많으면
# 어차피 사람이 봐야 하는 상황이다.
RECIPIENT_LOOKUP_MAX = 8

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000   # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게

# processing_status 코드 -> 사람이 읽는 상태 이름 (실측한 코드만 확실하고,
# 2는 1(결제완료)과 3(배송중) 사이라 상품준비중으로 추정해 넣었다).
PROCESSING_STATUS_NAMES = {1: "결제완료", 2: "상품준비중", 3: "배송중",
                           4: "배송완료", 5: "구매확정"}
# 반품/환불/취소 계열임을 뜻하는 sub_status (실측: 반품완료 건이 44).
CANCELLED_SUB_STATUS = 44

# 기다리면 송장이 나오는 상태에 들어 있는 말.
NOT_YET_KEYWORDS = ("결제", "준비", "입금", "예정")
# 아무리 기다려도 (이 송장을 올리면 안 되는) 상태에 들어 있는 말. 교환은
# 재배송 송장이 새로 달리지만 원 송장을 올리면 안 되는 것은 같다.
CANCELLED_KEYWORDS = ("취소", "반품", "교환", "환불", "품절")

# 주문목록 캐시. 컨텍스트(=이번 실행의 브라우저)별로 담는다. 한 공급사는
# 스레드 하나가 맡으므로 잠금은 필요 없다 (29CM와 동일).
_orders_cache: dict[int, list[dict]] = {}
# 이 컨텍스트로 지금까지 보낸 API 요청 수. get_tracking이 호출 전후를 비교해
# '이번 주문은 캐시만으로 답했다'(sent_request=False)를 알아내는 데 쓴다 -
# 오케스트레이터는 요청을 안 보낸 주문 뒤에 간격(1.5~4초)을 두지 않는다.
_request_count: dict[int, int] = {}


def _login_id() -> str | None:
    return os.environ.get("4910_ID")


def _login_pw() -> str | None:
    return os.environ.get("4910_PW")


# --------------------------------------------------------------------------
# 인증 (JWT 쿠키 <-> authorization 헤더)
# --------------------------------------------------------------------------

def _jwt_token(context: BrowserContext) -> str | None:
    """storage_state로 실려온 ably-jwt-token 쿠키. 없으면 None(=로그인 필요)."""
    try:
        for cookie in context.cookies("https://4910.kr"):
            if cookie.get("name") == JWT_COOKIE and cookie.get("value"):
                return str(cookie["value"])
    except Exception:  # noqa: BLE001 - 쿠키를 못 읽으면 로그인 경로로 가면 된다
        pass
    return None


def _api_headers(token: str) -> dict[str, str]:
    """x-app-type: AGLO가 핵심이다 - 없으면 에이블리 앱 주문이 나온다(실측)."""
    return {
        "authorization": f"JWT {token}",
        "x-app-type": "AGLO",
        "x-device-type": "PCWeb",
        "x-app-version": "0.1.0",
    }


def _looks_like_login_page(page) -> bool:
    return urlparse(page.url).path.startswith("/login")


def _auto_login(page) -> bool:
    """4910_ID/PW로 이메일 로그인을 자동 진행한다. 비밀번호가 없으면 False."""
    login_id = _login_id()
    login_pw = _login_pw()
    if not login_id or not login_pw:
        return False

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    try:
        page.locator(LOGIN_EMAIL_SELECTOR).fill(login_id)
        page.locator(LOGIN_PW_SELECTOR).fill(login_pw)
        page.get_by_role("button", name="로그인").first.click()

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            page.wait_for_timeout(1000)
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"4910 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1000

        raise BlockedError(
            "4910 로그인 후에도 로그인 화면에서 벗어나지 못했습니다 "
            "(아이디/비밀번호가 맞는지 확인해주세요).")
    finally:
        page.remove_listener("dialog", _on_dialog)


def _ensure_logged_in(context: BrowserContext, headless: bool) -> str:
    """로그인해서 JWT 토큰을 돌려준다. 성공하면 storage_state를 저장한다."""
    if headless and not _login_pw():
        raise BlockedError(
            "4910 로그인이 필요합니다. .env에 4910_ID/4910_PW를 넣어두면 자동으로 "
            "로그인합니다. 아니면 --headless 없이 실행해 수동으로 로그인해주세요.")

    page = context.new_page()
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(LOGIN_PW_SELECTOR, state="attached",
                                   timeout=common.RENDER_WAIT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - 폼이 안 뜨는 두 경우를 가른다
            # 이미 로그인된 세션이면 로그인 화면이 홈으로 넘어가버린다 - 그때는
            # 쿠키가 있어야 정상이고, 쿠키도 없이 폼도 없으면 구조가 바뀐 것이다.
            token = _jwt_token(context)
            if not _looks_like_login_page(page) and token:
                return token
            raise BlockedError(
                "4910 로그인 화면에 입력창이 나타나지 않습니다 (화면 구조가 바뀐 것으로 보입니다).") from exc

        if _auto_login(page):
            common.safe_print("[4910] 로그인 세션이 없어 자동으로 로그인했습니다.")
        else:
            if headless:  # pragma: no cover - 위에서 걸러지지만 방어적으로 남긴다
                raise BlockedError("4910 로그인이 필요합니다. --headless 없이 실행해주세요.")
            common.prefill_login_id(page, page.locator(LOGIN_EMAIL_SELECTOR), _login_id())
            common.safe_print("[4910] 뜬 브라우저 창에서 로그인해주세요 (최대 5분 대기).")
            if not common.wait_for_manual_login(
                    page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS):
                raise BlockedError("4910 로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")

        token = _jwt_token(context)
        if not token:
            raise BlockedError("4910 로그인은 지나갔는데 인증 쿠키가 없습니다 (화면 구조가 바뀐 것으로 보입니다).")
        context.storage_state(path=str(browser_mod.state_path(SITE_KEY)))
        return token
    finally:
        page.close()


# --------------------------------------------------------------------------
# API 조회 (주문목록 캐시 / 주문상세 폴백 / 배송조회)
# --------------------------------------------------------------------------

def _api_get(context: BrowserContext, url: str, headless: bool) -> tuple[int, dict | None]:
    """(상태코드, JSON)을 돌려준다. 401이면 재로그인 한 번 뒤 다시 시도한다."""
    _request_count[id(context)] = _request_count.get(id(context), 0) + 1
    token = _jwt_token(context)
    if token is None:
        token = _ensure_logged_in(context, headless)
    resp = context.request.get(url, headers=_api_headers(token))
    if resp.status == 401:
        token = _ensure_logged_in(context, headless)
        resp = context.request.get(url, headers=_api_headers(token))
        if resp.status == 401:
            raise BlockedError("4910 로그인 후에도 여전히 인증이 거부됩니다.")
    if "json" not in resp.headers.get("content-type", ""):
        raise ParseError(f"4910 API가 JSON이 아닌 응답을 주었습니다 (HTTP {resp.status}).")
    if resp.status == 404:
        return 404, None
    if not resp.ok:
        raise ParseError(f"4910 API 조회가 실패했습니다 (HTTP {resp.status}, {url}).")
    return resp.status, resp.json()


def _load_orders(context: BrowserContext, headless: bool) -> list[dict]:
    """주문목록 전체(최대 LIST_MAX_PAGES 페이지)를 한 번만 읽어 캐시한다."""
    cached = _orders_cache.get(id(context))
    if cached is not None:
        return cached
    orders: list[dict] = []
    for page_no in range(1, LIST_MAX_PAGES + 1):
        _, data = _api_get(context, LIST_URL.format(page=page_no), headless)
        if data is None:
            break
        orders.extend(data.get("orders") or [])
        if page_no >= int(data.get("max_page_number") or 1):
            break
    _orders_cache[id(context)] = orders
    return orders


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """오케스트레이터가 첫 조회 전에 한 번 불러준다 - 주문목록을 미리 읽어둔다.

    실패해도 예외를 내보내지 않는다 - get_tracking이 어차피 같은 목록을
    스스로 읽는다(그때 나는 예외가 주문별 실패로 정리된다).
    """
    try:
        loaded = _load_orders(context, headless)
        common.safe_print(f"[4910] 주문목록 {len(loaded)}건을 미리 읽었습니다.")
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 주문별 조회에서 다시 시도한다
        common.safe_print(f"[4910] 주문목록을 미리 읽지 못했습니다 ({e}).")


def _fetch_detail(context: BrowserContext, order_no: str, headless: bool) -> dict:
    """주문상세. 목록(최대 100건)에 안 잡히는 오래된 주문의 폴백."""
    status, data = _api_get(context, DETAIL_URL.format(order_no=order_no), headless)
    if status == 404 or data is None:
        raise OrderNotFound(f"4910에 이 주문번호가 없습니다 (주문번호={order_no}).")
    return data


def _fetch_tracking(context: BrowserContext, item_sno: int, headless: bool) -> tuple[str, str]:
    """배송조회 API로 (송장번호, 택배사)를 읽는다."""
    status, data = _api_get(context, TRACKING_URL.format(item_sno=item_sno), headless)
    if status == 404 or data is None:
        raise ParseError(f"4910 배송조회가 없다고 합니다 (주문상품번호={item_sno}).")
    info = data.get("delivery_info") or {}
    invoice = str(info.get("invoice") or "").strip()
    courier = str((info.get("delivery") or {}).get("name") or "").strip()
    if not invoice:
        raise TrackingNotAvailableYet(f"배송조회에 아직 송장번호가 없습니다 (주문상품번호={item_sno}).")
    return invoice, common.normalize_courier(courier)


# --------------------------------------------------------------------------
# 주문/상품 고르기
# --------------------------------------------------------------------------

def parse_product_url(product_url: str) -> tuple[str, str | None]:
    """상품URL을 (형태, 값)으로 푼다.

    ("order", 주문번호) / ("goods", 상품번호) / ("none", None)
    """
    segments = [s for s in urlparse(product_url).path.split("/") if s]
    for i, seg in enumerate(segments):
        if seg == "order" and i + 1 < len(segments) and segments[i + 1].isdigit():
            return "order", segments[i + 1]
        if seg == "goods" and i + 1 < len(segments) and segments[i + 1].isdigit():
            return "goods", segments[i + 1]
    return "none", None


def _status_text(item: dict) -> str:
    """이 상품의 상태 표기. 안내 문구가 있으면 그쪽이 더 구체적이다."""
    message = str(item.get("delivery_information_message") or "").strip()
    name = PROCESSING_STATUS_NAMES.get(item.get("processing_status"), "알 수 없음")
    if item.get("processing_sub_status") == CANCELLED_SUB_STATUS and not message:
        message = "반품/환불"
    return f"{name} {message}".strip() if message else name


def _is_cancelled(item: dict) -> bool:
    if item.get("processing_sub_status") == CANCELLED_SUB_STATUS:
        return True
    return any(k in _status_text(item) for k in CANCELLED_KEYWORDS)


def _option_text(item: dict) -> str:
    """옵션 표기("블랙 XL"). 상품명까지 붙여서 비교 폭을 넓힌다 (29CM와 동일)."""
    options = " ".join(str(o) for o in (item.get("options") or []) if o)
    name = str((item.get("goods") or {}).get("name") or "").strip()
    return f"{name} {options}".strip()


def _match_option(candidates: list[tuple[dict, dict]],
                  order_option: str | None) -> list[tuple[dict, dict]]:
    """샵마인 엑셀의 "주문옵션"과 맞는 후보만 남긴다. 못 좁히면 그대로 둔다."""
    target = normalize_option(order_option)
    if not target or len(candidates) <= 1:
        return candidates
    matched = []
    for order, item in candidates:
        value = normalize_option(_option_text(item))
        if value and (value == target or value in target or target in value):
            matched.append((order, item))
    return matched or candidates


def _match_recipient(context: BrowserContext, candidates: list[tuple[dict, dict]],
                     recipient_name: str | None, headless: bool) -> list[tuple[dict, dict]]:
    """수령인 이름으로 좁힌다 - 주문상세(receiver_name)를 후보마다 열어본다.

    같은 상품을 같은 옵션으로 여러 고객에게 사주는 경우, 옵션으로는 절대
    갈리지 않아서 이게 마지막 열쇠다. 후보가 너무 많으면(RECIPIENT_LOOKUP_MAX
    초과) 상세를 다 열지 않고 그대로 돌려준다 - 어차피 사람이 봐야 한다.
    """
    wanted = (recipient_name or "").replace(" ", "")
    if not wanted or len(candidates) <= 1 or len(candidates) > RECIPIENT_LOOKUP_MAX:
        return candidates
    matched = []
    for order, item in candidates:
        try:
            detail = _fetch_detail(context, str(order.get("sno")), headless)
        except AdapterError:
            continue
        receiver = str(detail.get("receiver_name") or "").replace(" ", "")
        if receiver and receiver == wanted:
            matched.append((order, item))
    return matched or candidates


def _find_candidates(context: BrowserContext, product_url: str, headless: bool,
                     order_option: str | None,
                     recipient_name: str | None) -> list[tuple[dict, dict]]:
    """상품URL로 (주문, 주문상품) 후보를 모아 하나로 좁혀본다."""
    kind, value = parse_product_url(product_url)
    orders = _load_orders(context, headless)

    if kind == "order":
        order = next((o for o in orders if str(o.get("sno")) == value), None)
        if order is None:
            order = _fetch_detail(context, value, headless)  # 목록 밖(오래된 주문) 폴백
        candidates = [(order, item) for item in (order.get("order_items") or [])]
        if not candidates:
            raise ParseError(f"4910 주문 응답에 상품이 없습니다 (주문번호={value}).")
        return _match_option(candidates, order_option)

    if kind == "goods":
        candidates = [(o, item) for o in orders for item in (o.get("order_items") or [])
                      if str((item.get("goods") or {}).get("sno")) == value]
        if not candidates:
            raise OrderNotFound(
                f"4910 주문내역(최근 {LIST_MAX_PAGES * 10}건)에 이 상품이 든 주문이 없습니다 "
                f"(상품번호={value}).")
    else:
        # 주소만으로는 아무것도 특정할 수 없다 - 주문옵션이 유일한 단서다.
        if not normalize_option(order_option):
            raise ParseError(
                f"이 상품URL로는 주문을 특정할 수 없고 주문옵션도 비어 있습니다: {product_url}")
        candidates = [(o, item) for o in orders for item in (o.get("order_items") or [])]

    candidates = _match_option(candidates, order_option)
    if len(candidates) > 1:
        # 취소/반품된 줄보다 살아 있는 줄이 우선이다.
        live = [(o, i) for o, i in candidates if not _is_cancelled(i)]
        if live:
            candidates = live
    candidates = _match_recipient(context, candidates, recipient_name, headless)
    if not candidates:
        raise OrderNotFound(f"주문옵션({order_option})과 맞는 4910 주문을 찾지 못했습니다.")
    return candidates


def _order_date_of(order: dict) -> date | None:
    """주문일 (ordered_at "2026-09-02 07:42"). 못 읽으면 None."""
    raw = str(order.get("ordered_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _delivery_note_of(item: dict) -> str | None:
    """'9/2(수) 이내 발송 예정' 같은 안내 문구. 예정 문구가 아니면 싣지 않는다."""
    message = str(item.get("delivery_information_message") or "").strip()
    return message if "예정" in message else None


def _tracking_from_candidates(context: BrowserContext, candidates: list[tuple[dict, dict]],
                              order_no: str, headless: bool) -> TrackingResult:
    live = [(o, i) for o, i in candidates if not _is_cancelled(i)]
    if not live:
        statuses = " / ".join(_status_text(i) for _, i in candidates)
        raise OrderCancelled(
            f"주문 상태가 {statuses} 입니다 (주문번호={order_no}) - 취소/반품/교환 주문인지 확인해주세요.")

    shipped = [(o, i) for o, i in live if i.get("delivery_post_sno")]
    if not shipped:
        statuses = " / ".join(_status_text(i) for _, i in live)
        raise TrackingNotAvailableYet(
            f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={statuses}).")

    # 같은 배송 건(delivery_post_sno)이면 배송조회를 두 번 부를 이유가 없다.
    by_post: dict[int, dict] = {}
    for _, item in shipped:
        by_post.setdefault(int(item["delivery_post_sno"]), item)
    found = {
        _fetch_tracking(context, int(item["sno"]), headless)
        for item in by_post.values()
    }
    if len(found) > 1:
        raise ParseError(
            f"후보 주문/상품마다 송장번호가 다릅니다 (주문번호={order_no}) - "
            "주문옵션이나 수령인으로 특정하지 못했으니 직접 확인해주세요.")
    tracking_no, courier = next(iter(found))
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True,
    order_option: str | None = None, recipient_name: str | None = None,
) -> TrackingResult:
    # 요청 수 전후 비교로 '캐시(미리 읽은 주문목록)만으로 답했는가'를 알아낸다.
    before = _request_count.get(id(context), 0)

    def _sent() -> bool:
        return _request_count.get(id(context), 0) > before

    try:
        candidates = _find_candidates(context, product_url, headless, order_option, recipient_name)
        order = candidates[0][0]
        order_no = str(order.get("sno") or "?")
        result = attach_order_date(
            _order_date_of(order),
            lambda: _tracking_from_candidates(context, candidates, order_no, headless),
            delivery_note=_delivery_note_of(candidates[0][1]),
        )
    except AdapterError as e:
        e.sent_request = _sent()
        raise
    result.sent_request = _sent()
    return result
