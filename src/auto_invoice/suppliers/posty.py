"""포스티(posty.kr) 공급사 어댑터.

리버스엔지니어링 결과(2026-08-31 실측):
- 주문상세 URL: https://posty.kr/checkout/orders/<주문번호> (샵마인 엑셀의
  "상품URL"에 이 형태로 들어온다. 주문번호는 18자리 숫자다.)
- 화면은 Next.js로 그리고 데이터는 전부 GraphQL로 받는다. 주문상세 화면에는
  **택배사도 송장번호도 나오지 않고**("배송현황" 버튼을 눌러야 나온다), 그
  값을 그리는 화면(/checkout/order-item/<주문상품번호>/shipping-tracking)은
  서버렌더라 __NEXT_DATA__에 들어 있다. 화면을 두 번 여는 대신
    POST https://posty.kr/api/2/graphql/GetShippingGroupList
    {"query": ..., "variables": {"order_number": "<주문번호>"}}
  하나만 부른다 - 응답의 order_item_list에 shipping_company(택배사 코드)와
  invoice_number(송장번호)가 주문상태·옵션·주문일과 같이 들어 있다. 그래서
  이 어댑터는 페이지를 아예 열지 않는다(조회 한 건에 요청 하나).
- 택배사는 "CJ"/"LOGEN" 같은 **코드**로 온다. 코드->한글 이름 표는 배송현황
  화면이 통째로 내려주는 shipping_company_list를 그대로 옮겨 적었다
  (SHIPPING_COMPANY_NAMES). 표에 없는 코드는 코드 그대로 두고, 마지막에
  common.normalize_courier로 샵마인 표기에 맞춘다.
- 로그인은 POST /api/2/graphql/Login (이메일/비밀번호 평문)인데, **API만
  직접 부르면 통과하지 못한다**:
    · 번들 Chromium의 기본 UA(HeadlessChrome)로 부르면 "invalid session"
    · 정상 UA로 불러도 페이지를 거치지 않으면 "잘못된 접근입니다"
      (로그인 화면의 자바스크립트가 심는 POSTY_FINGERPRINT 쿠키가 없다)
  그래서 로그인만 **평범한 UA를 준 별도 컨텍스트에서 로그인 화면을 실제로
  열어** 처리하고, 성공하면 쿠키만 원래 컨텍스트로 옮긴다. 창을 띄울 필요는
  없다(headless로 통과하는 것을 확인했다) - 현대몰/W컨셉처럼 진짜 크롬을
  띄우는 것과는 다르다.
- 한 번 로그인하면 쿠키(ZIBETID/connect.sid)가 auth/posty_state.json에
  저장되어 다음 실행부터는 로그인 없이 조회만 한다(browser.save_state).
- 주문상태(status)는 영문 코드다. 실측한 값은 AWAITING_SHIPMENT(배송준비중),
  SHIPMENT_PROCESS_REQUESTED(발송처리 요청됨 - 화면엔 배송준비중으로 보인다),
  CONFIRMED(구매확정), CANCELLED(취소완료), RETURNED(반품완료)이고, 나머지는
  같은 계열 이름으로 넣어뒀다. 취소/반품 건은 송장번호가 남아 있어도(반품
  주문에는 처음 나갈 때의 송장이 그대로 있다) 올리면 안 되므로 상태를 먼저
  본다.
- 한 주문에 상품이 여러 개면 샵마인 엑셀의 "주문옵션"으로 어느 상품인지
  고른다(product_info.options가 "블랙 / L" 형태로 같은 표기다). 특정하지
  못하면 다른 어댑터와 같은 안전 규칙으로, 송장번호가 전부 같을 때만 쓰고
  다르면 사람이 확인하도록 예외를 던진다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    raise_if_delayed_any,
    attach_order_date,
    normalize_option,
)

load_dotenv()

DOMAINS = {"posty.kr", "www.posty.kr", "m.posty.kr"}
SITE_KEY = "posty"

GRAPHQL_URL = "https://posty.kr/api/2/graphql/{op}"
LOGIN_URL = "https://posty.kr/auth/login-email"
ORDER_URL = "https://posty.kr/checkout/orders/{order_no}"

# 로그인 화면을 여는 컨텍스트에만 준다 - 기본 UA(HeadlessChrome)로 로그인을
# 부르면 사이트가 "invalid session"으로 막는다. 조회(GraphQL)는 기본 UA로도
# 정상 응답하는 것을 확인했으므로 원래 컨텍스트는 건드리지 않는다.
LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000
# 로그인 화면을 열고 **최소 이만큼 머문 뒤에** 제출해야 한다. 화면을 열자마자
# 제출하면 사이트가 "잘못된 접근입니다"(auth_invalid_access)로 거부한다 -
# 아이디/비밀번호와 무관하고, 화면에 머문 시간만의 문제다(2026-08-31 실측:
# 1.5초 0/1 성공, 3초 1/2, 4초 이상 성공, 6초 3/3 성공). 사람이 직접 칠 때는
# 자연스럽게 넘는 시간이라 6초로 잡았다.
LOGIN_PAGE_SETTLE_MS = 6000
# 그래도 거부당하면 화면부터 다시 열어 한 번 더 해본다. 비밀번호가 틀린
# 경우까지 반복하면 계정이 잠길 수 있으므로, 아래 '봇 확인' 코드일 때만이다.
LOGIN_RETRY_COUNT = 2

# 로그인이 필요하다는 뜻으로 사이트가 주는 에러 코드.
LOGIN_REQUIRED_CODES = {"route_not_logged_in", "invalid_session", "auth_invalid_access"}
# 계정 문제가 아니라 '봇으로 보인다'는 뜻의 코드. 이때만 다시 시도한다.
LOGIN_RETRYABLE_CODES = {"auth_invalid_access", "invalid_session"}

ORDER_QUERY = """query GetShippingGroupList($order_number: String!) {
  shipping_group_list(order_number: $order_number) {
    item_list {
      order { order_number date_created }
      order_item_list(exclude_new_exchange_order_item: true) {
        order_item_number
        status
        item_status
        quantity
        shipping_company
        invoice_number
        product_info { name options }
        order_item_product { option_detail_list { name value } }
        fulfillment_info { date_expected_arrival_text }
      }
    }
  }
}"""

# 배송현황 화면이 통째로 내려주는 코드->택배사 이름 표(shipping_company_list) 그대로.
SHIPPING_COMPANY_NAMES = {
    "CJ": "CJ대한통운",
    "POSTAL": "우체국",
    "HANJIN": "한진택배",
    "LOGEN": "로젠택배",
    "LOTTE": "롯데택배",
    "LOGIS": "일양로지스",
    "DAESIN": "대신택배",
    "KDEXP": "경동택배",
    "HDEXP": "합동택배",
    "CHUNIL": "천일택배",
    "CVSNET": "편의점 택배",
    "HPL": "한의사랑택배",
    "KUNYOUNG": "건영택배",
    "HONAM": "호남택배",
    "SLX": "SLX",
    "BGF": "BGF포스트",
    "NHLOGIS": "농협택배",
    "HOMEPICK": "홈픽택배",
    "KOREXG": "CJ대한통운국제특송",
    "LOTTEGLOBAL": "롯데글로벌",
    "HANDEX": "한덱스",
    "DHL": "DHL",
    "FEDEX": "FEDEX",
    "TNT": "TNT",
    "USPS": "USPS",
    "GSMNTON": "GSM NtoN",
    "SWGEXP": "성원글로벌",
    "ACIEXPRESS": "ACI Express",
    "AIRBOY": "에어보이익스프레스",
    "KGLNET": "KGL네트웍스",
    "LINEEXPRESS": "LineExpress",
    "TWOFASTEXP": "2fast익스프레스",
    "GSIEXPRESS": "GSI익스프레스",
    "DHLGLOBALMAIL": "DHL GlobalMail",
    "GPSLOGIX": "GPS로직",
    "CRLX": "시알로지텍",
    "CWAY": "씨웨이",
    "ACEEXP": "ACE Express",
    "WARPEX": "워펙스",
    "SMARTLOGIS": "스마트로지스",
    "ESTHER": "에스더쉬핑",
    "INTRAS": "로토스",
    "EUNHA": "은하쉬핑",
    "TPMLOGIS": "티피엠코리아",
    "ZENIELSYSTEM": "제니엘시스템",
    "TODAYPICKUP": "카카오T당일배송",
    "DAERIM": "대림통운",
    "LOGISPARTNER": "로지스파트너",
    "KSE": "국제익스프레스",
    "DRABBIT": "딜리래빗",
    "DOOBALHERO": "두발히어로",
    "VROONG": "부릉",
    "ETOMARS": "이투마스",
    "PINGPONG": "핑퐁",
    "KOREAYOGURT": "한국야쿠르트",
    "TODAY": "투데이",
    "LOTTECHILSUNG": "롯데칠성",
    "DNDN": "든든택배",
    "BRIDGE": "브리지로지스",
    "JCLS": "JCLS",
    "ETC": "기타택배",
}

# 기다려도 송장번호가 나올 수 없는 상태 (사람이 샵마인에서 직접 처리해야 한다).
# 반품/교환 건은 처음 나갈 때의 송장번호가 그대로 남아 있어서, 송장번호가
# 있는지보다 상태를 **먼저** 봐야 한다.
CANCELLED_STATUSES = {
    "CANCELLED": "취소완료",
    "CANCEL_REQUESTED": "취소요청",
    "RETURNED": "반품완료",
    "RETURN_REQUESTED": "반품요청",
    "EXCHANGED": "교환완료",
    "REJECTED": "주문거부",
    "REFUNDED": "환불완료",
    "OUT_OF_STOCK": "품절",
}

# 아직 발송 전이라 송장이 없는 것이 정상인 상태.
NOT_YET_STATUSES = {
    "BEFORE_DEPOSIT": "입금대기",
    "PAYMENT_COMPLETE": "결제완료",
    "PAYMENT_COMPLETED": "결제완료",
    "AWAITING_SHIPMENT": "배송준비중",
    "PREPARING_SHIPMENT": "배송준비중",
    # 발송처리를 요청해둔 상태 - 화면에는 배송준비중으로 보이고 송장은 아직
    # 없다 (2026-09-01 실측: 실패로 잘못 기록되던 상태).
    "SHIPMENT_PROCESS_REQUESTED": "배송준비중",
    "SHIPMENT_HOLD": "발송보류",
}

STATUS_LABELS = {**CANCELLED_STATUSES, **NOT_YET_STATUSES,
                 "SHIPPING": "배송중", "DELIVERED": "배송완료", "CONFIRMED": "구매확정"}

KST = timezone(timedelta(hours=9))


def extract_order_no(product_url: str) -> str:
    """상품URL에서 포스티 주문번호를 뽑는다 (/checkout/orders/<주문번호>)."""
    parts = [p for p in urlparse(product_url).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "checkout" and parts[1] == "orders" and parts[2].isdigit():
        return parts[2]
    raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")


def _graphql(context: BrowserContext, op: str, query: str, variables: dict,
             referer: str) -> dict:
    """포스티 GraphQL 한 번 호출. 응답 본문(dict)을 그대로 돌려준다."""
    response = context.request.post(
        GRAPHQL_URL.format(op=op),
        data={"query": query, "variables": variables},
        headers={"content-type": "application/json", "origin": "https://posty.kr",
                 "referer": referer},
    )
    if not response.ok:
        raise ParseError(f"포스티 API가 응답하지 않았습니다 (HTTP {response.status}).")
    try:
        return response.json() or {}
    except Exception as exc:  # noqa: BLE001 - 본문이 JSON이 아니면 구조가 바뀐 것이다
        raise ParseError(f"포스티 API 응답을 해석하지 못했습니다: {exc}") from exc


def _error_codes(body: dict) -> set[str]:
    return {(e.get("extensions") or {}).get("code") for e in (body.get("errors") or [])}


def _needs_login(body: dict) -> bool:
    return bool(_error_codes(body) & LOGIN_REQUIRED_CODES)


def _login_error_message(body: dict) -> str:
    """사이트가 준 실패 사유. 없으면 알려주지 않았다고 적는다."""
    login = ((body.get("data") or {}).get("login")) or {}
    if login.get("error_message"):
        return str(login["error_message"])
    for error in body.get("errors") or []:
        extensions = error.get("extensions") or {}
        if extensions.get("description"):
            return str(extensions["description"])
        if error.get("message"):
            return str(error["message"])
    return "사유를 알려주지 않았습니다."


def _auto_login(context: BrowserContext) -> None:
    """POSTY_ID/POSTY_PW로 자동 로그인하고 쿠키를 원래 컨텍스트에 옮긴다.

    로그인만 별도 컨텍스트에서 하는 이유는 이 파일 맨 위 docstring 참고
    (기본 UA로는 막히고, 화면을 거치지 않으면 fingerprint 쿠키가 없다).
    """
    login_id = os.environ.get("POSTY_ID")
    login_pw = os.environ.get("POSTY_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "포스티 로그인이 필요하지만 POSTY_ID/POSTY_PW 환경변수가 설정되어 있지 "
            "않습니다. .env에 추가해주세요."
        )

    browser = context.browser
    if browser is None:
        raise BlockedError("포스티 로그인용 브라우저를 찾지 못했습니다.")

    login_context = browser.new_context(
        viewport=browser_mod.MOBILE_VIEWPORT, user_agent=LOGIN_USER_AGENT,
        locale="ko-KR", timezone_id="Asia/Seoul")
    try:
        page = login_context.new_page()
        for attempt in range(1, LOGIN_RETRY_COUNT + 1):
            body = _submit_login(page, login_id, login_pw)
            if body is None:
                # 30초 안에 응답도 이동도 없었다 - 서버가 느렸을 수 있으니
                # 화면부터 다시 열어 한 번 더 해본다 (비밀번호 오류와 무관한
                # 실패라 계정이 잠길 걱정은 없다).
                if attempt < LOGIN_RETRY_COUNT:
                    common.safe_print("[posty] 로그인 응답이 없어 다시 시도합니다.")
                    continue
                raise BlockedError("포스티 자동 로그인 결과를 30초 안에 확인하지 못했습니다.")
            if (((body.get("data") or {}).get("login")) or {}).get("success"):
                # 로그인 응답의 Set-Cookie가 컨텍스트에 반영될 틈을 준다.
                page.wait_for_timeout(1000)
                context.add_cookies(login_context.cookies())
                common.safe_print("[posty] 자동 로그인에 성공했습니다.")
                return
            message = _login_error_message(body)
            if attempt < LOGIN_RETRY_COUNT and _error_codes(body) & LOGIN_RETRYABLE_CODES:
                common.safe_print(f"[posty] 로그인이 거부되어 다시 시도합니다 ({message}).")
                continue
            raise BlockedError(f"포스티 로그인 실패 - {message}")
    finally:
        try:
            login_context.close()
        except Exception:  # noqa: BLE001 - 컨텍스트를 못 닫아도 결과에 영향은 없다
            pass


def _submit_login(page, login_id: str, login_pw: str) -> dict | None:
    """로그인 화면을 처음부터 열어 제출하고, 사이트가 준 응답을 돌려준다.

    응답을 못 받았지만 로그인 페이지를 벗어났으면 성공으로 간주한 본문을,
    30초 동안 아무 일도 없었으면 None을 돌려준다(호출한 쪽이 재시도).
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # 화면에 충분히 머문 뒤에 제출해야 한다 (LOGIN_PAGE_SETTLE_MS 주석 참고).
    page.wait_for_timeout(LOGIN_PAGE_SETTLE_MS)

    email_box = page.locator("input[type='email']")
    password_box = page.locator("input[type='password']")
    if email_box.count() == 0 or password_box.count() == 0:
        raise BlockedError(
            "포스티 로그인 화면에서 입력창을 찾지 못했습니다 (화면 구조가 바뀐 것으로 보입니다).")

    # 폼을 자바스크립트가 관리해서(입력해야 로그인 버튼이 켜진다) 값을 넣는
    # 것이 아니라 실제로 타이핑한다.
    email_box.click()
    email_box.press_sequentially(login_id, delay=40)
    password_box.click()
    password_box.press_sequentially(login_pw, delay=40)
    page.wait_for_timeout(300)

    answer: dict = {}

    def _remember(response) -> None:
        if response.url.endswith("/graphql/Login"):
            try:
                answer["body"] = response.json()
            except Exception:  # noqa: BLE001 - 본문을 못 읽으면 시간초과로 끝난다
                pass

    page.on("response", _remember)
    try:
        page.get_by_role("button", name="로그인").first.click()
        elapsed_ms = 0
        while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
            page.wait_for_timeout(500)
            elapsed_ms += 500
            if answer.get("body") is not None:
                return answer["body"]
            # 응답 본문을 놓쳐도(클릭 직후 홈으로 이동하면서 캡처를 못 하는
            # 경우가 있었다 - 2026-09-01 실전에서 4건 전부 30초 타임아웃)
            # 로그인 페이지를 벗어났다면 성공이다. 실패하면 포스티는 이동
            # 없이 로그인 화면에 에러만 띄운다.
            if "/auth/" not in page.url:
                return {"data": {"login": {"success": True}}}
        return None  # 시간초과 - 호출한 쪽이 화면부터 다시 열어 한 번 더 해본다
    finally:
        page.remove_listener("response", _remember)


def _order_items(body: dict, order_no: str) -> list[dict]:
    groups = ((body.get("data") or {}).get("shipping_group_list") or {}).get("item_list")
    if groups is None:
        raise ParseError(f"포스티 주문 조회 응답 구조가 예상과 다릅니다 (주문번호={order_no}).")
    if not groups:
        raise OrderNotFound(f"포스티에 이 주문번호가 없습니다 (주문번호={order_no}).")
    items = [item for group in groups for item in (group.get("order_item_list") or [])]
    if not items:
        raise ParseError(f"주문에 상품이 하나도 없습니다 (주문번호={order_no}).")
    return items


def _order_date(body: dict):
    """주문일(order.date_created는 epoch 밀리초). 못 읽으면 None."""
    groups = ((body.get("data") or {}).get("shipping_group_list") or {}).get("item_list") or []
    for group in groups:
        created = (group.get("order") or {}).get("date_created")
        if created:
            try:
                return datetime.fromtimestamp(int(created) / 1000, KST).date()
            except Exception:  # noqa: BLE001 - 날짜 하나 때문에 조회가 깨지면 안 된다
                return None
    return None


def _delivery_note(items: list[dict]) -> str | None:
    """'내일(화) 이내 발송 예정' 같은 안내 문구. 없으면 None."""
    for item in items:
        text = ((item.get("fulfillment_info") or {}).get("date_expected_arrival_text") or "").strip()
        if text:
            return text
    return None


def _item_options(item: dict) -> str:
    """이 상품의 옵션 표기. product_info.options("블랙 / L")를 먼저 쓴다."""
    options = ((item.get("product_info") or {}).get("options") or "").strip()
    if options:
        return options
    details = (item.get("order_item_product") or {}).get("option_detail_list") or []
    return " / ".join(str(d.get("value") or "").strip() for d in details if d.get("value"))


def _status_of(item: dict) -> str:
    return str(item.get("item_status") or item.get("status") or "").strip().upper()


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "알 수 없음")


def _labels_of(items: list[dict]) -> str:
    return " / ".join(_status_label(_status_of(item)) for item in items)


def _select_items_by_order_option(items: list[dict], order_option: str | None) -> list[dict] | None:
    """샵마인 엑셀의 "주문옵션"과 같은 옵션이 적힌 상품이 딱 하나면 그것만.

    표기가 사이트마다 조금씩 달라서(", " vs " / ") normalize_option으로
    공백·구분자를 지우고 비교하고, 한쪽이 다른 쪽을 포함하는 경우도 같은
    것으로 본다. 후보가 0개거나 2개 이상이면 None - 호출한 쪽이 송장번호를
    서로 비교하는 안전 규칙으로 넘어간다.
    """
    target = normalize_option(order_option)
    if not target or len(items) <= 1:
        return None
    candidates = []
    for item in items:
        value = normalize_option(_item_options(item))
        if value and (value == target or value in target or target in value):
            candidates.append(item)
    return candidates if len(candidates) == 1 else None


def _courier_name(code: str | None) -> str | None:
    """택배사 코드를 샵마인이 아는 이름으로. 모르는 코드는 코드 그대로 둔다."""
    if not code:
        return None
    raw = SHIPPING_COMPANY_NAMES.get(code.strip().upper(), code.strip())
    return common.normalize_courier(raw)


def _raise_not_shipped(items: list[dict], order_no: str) -> None:
    """송장번호가 없는 상품들의 상태를 보고 '아직 미발급'인지 '취소'인지 가른다.

    상태 코드를 정확히 읽을 수 있는 사이트라 화면 전체 텍스트는 보지 않는다
    (base.py 주석의 '주문상태를 정확히 읽을 수 있는 공급사' 규칙). 한 주문에
    취소된 상품과 준비중인 상품이 섞여 있으면 준비중 쪽이 이긴다 - 기다리면
    송장이 나오는 주문을 사람이 처리할 목록으로 보내지 않기 위해서다.
    """
    statuses = [_status_of(item) for item in items]
    labels = _labels_of(items)
    if any(s in NOT_YET_STATUSES for s in statuses):
        raise TrackingNotAvailableYet(
            f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={labels}).")
    # 지연 상태 코드는 실측한 적이 없어 이름만으로 잡는다 (예: SHIPMENT_DELAYED).
    if any("DELAY" in s for s in statuses):
        raise_if_delayed_any(["지연"], order_no)
    if any(s in CANCELLED_STATUSES for s in statuses):
        raise OrderCancelled(
            f"주문 상태가 {labels} 입니다 (주문번호={order_no}) - 취소/반품 주문인지 확인해주세요.")
    raise ParseError(
        f"송장번호가 비어 있는데 상태를 알 수 없습니다 (주문번호={order_no}, 상태={labels}).")


def _tracking_from_items(items: list[dict], order_no: str,
                         order_option: str | None) -> TrackingResult:
    # 옵션으로 어느 상품인지 확정되면 그것만 본다 - 같은 주문의 다른 상품이
    # 이미 나갔더라도, 우리가 올려야 하는 상품의 송장이 아니면 안 된다.
    matched = _select_items_by_order_option(items, order_option)
    if matched is not None:
        items = matched

    # 취소/반품 건은 처음 나갈 때의 송장번호가 그대로 남아 있어서, 송장번호가
    # 있는지보다 상태를 먼저 본다.
    live = [item for item in items if _status_of(item) not in CANCELLED_STATUSES]
    if not live:
        raise OrderCancelled(
            f"주문 상태가 {_labels_of(items)} 입니다 (주문번호={order_no}) - "
            "취소/반품 주문인지 확인해주세요.")

    shipped = [item for item in live if str(item.get("invoice_number") or "").strip()]
    if not shipped:
        _raise_not_shipped(live, order_no)

    found = {(str(item["invoice_number"]).strip(), _courier_name(item.get("shipping_company")))
             for item in shipped}
    if len(found) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "상품별로 나눠 배송된 것으로 보입니다.")

    tracking_no, courier = next(iter(found))
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True,
    order_option: str | None = None,
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    referer = ORDER_URL.format(order_no=order_no)

    body = _graphql(context, "GetShippingGroupList", ORDER_QUERY,
                    {"order_number": order_no}, referer)
    if _needs_login(body):
        common.safe_print("[posty] 로그인 세션이 없어 자동 로그인을 시도합니다.")
        _auto_login(context)
        body = _graphql(context, "GetShippingGroupList", ORDER_QUERY,
                        {"order_number": order_no}, referer)
        if _needs_login(body):
            raise BlockedError("포스티 로그인 후에도 여전히 로그인이 필요하다고 나옵니다.")
    if body.get("errors"):
        raise ParseError(
            f"포스티 주문 조회가 실패했습니다 (주문번호={order_no}, "
            f"사유={_login_error_message(body)}).")

    items = _order_items(body, order_no)
    # 주문일과 예정 문구는 조회 결과(또는 예외)에 실어 보낸다 - 미발급인 채로
    # 며칠 지난 주문을 사람이 따로 챙기는 데 쓴다.
    return attach_order_date(
        _order_date(body),
        lambda: _tracking_from_items(items, order_no, order_option),
        delivery_note=_delivery_note(items),
    )
