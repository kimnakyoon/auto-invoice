"""NS홈쇼핑(m.nsmall.com) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://m.nsmall.com/cs/order-detail?orderNum=<주문번호>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고 orderNum만
  있으면 되도록 만들었다.)
- 로그인이 안 되어 있으면 https://m.nsmall.com/customer/login?joinRedirectUri=...
  으로 리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#userId, 비밀번호
  input#userPw, 로그인 버튼은 button.login-button. 사용자가 명시적으로 완전
  자동 로그인을 요청했고, 실제로 아이디+비밀번호를 채우고 로그인 버튼을 자동
  클릭해도 reCAPTCHA 등에 막히지 않는 것을 확인했다 (SSG/더현대와 동일한
  패턴). 그래서 NSMALL_ID/NSMALL_PW 환경변수로 완전 자동 로그인한다. 로그인
  세션은 storage_state(localStorage의 refresh_token)로 저장되지만 그 토큰의
  수명이 30분이라(2026-09-03 디코드), 실행 사이가 30분 넘게 벌어지면 첫 주문
  에서 자동 로그인을 다시 탄다 (같은 실행 안의 나머지 주문은 바로 조회된다).
  로그인 여부는 주소가 아니라 화면(주문번호 vs 비밀번호 입력창)으로 판정한다
  - 자세한 사연은 _wait_for_order_or_login 참고.
- 주문상세 페이지의 "배송조회" 버튼을 클릭하면 새 탭이 아니라 같은 페이지 위에
  모달(role=dialog, class에 modal-delivery-state 포함)이 뜨고, 그 안에서
  GET https://mapi.nsmall.com/or/api/v1/order/order/order-dlvr-detail-with-gift
  요청이 호출된다. 이 응답의 data.resultData.dlvrTrackingInfoList[0].wblNum이
  송장번호, .lscNm이 택배사명(이미 "롯데택배"처럼 정식 명칭으로 오는 경우도
  있지만, orderItems.dlvrEntCdNm 쪽은 "롯데"처럼 축약형으로 오는 걸 확인했다 -
  사용자가 요청한 대로 축약형/코드를 정식 명칭으로 맞추는 정규화를 lscNm에도
  동일하게 적용해둔다).
- 모달은 button.layer-close-bt로 닫아야 다음 "배송조회" 버튼을 클릭할 수 있다
  (모달이 열린 채로는 뒤에 있는 버튼이 안 눌린다 - 더현대 어댑터와 동일).
- 상품이 여러 개라 "배송조회" 버튼이 여러 개 뜨는 경우, 샵마인 엑셀의
  "주문옵션" 값으로 어느 버튼인지 특정할 수 있으면 그 버튼만 클릭한다.
  특정할 수 없으면(다른 어댑터와 동일한 안전 규칙) 전부 클릭해서 실제로 서로
  다른 송장인지 비교하고, 다르면 사람이 확인하도록 예외를 던진다.
- 아직 발송 전 상태 문구(NOT_YET_PATTERNS): 실제 미발송 주문 화면에서 "상품 준비
  중"(공백 포함)을 확인했다. 화면 표기가 공백 유무 등으로 조금씩 다를 수 있어
  normalize_option으로 공백/구분자를 지우고 비교한다. 나머지 값들("결제완료",
  "배송준비중", "주문접수")은 다른 어댑터에서 흔히 보이는 값으로 추정해둔 것이라
  다르게 나오면 조정이 필요하다.
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

LOGIN_ID_SELECTOR = "#userId"
LOGIN_PW_SELECTOR = "#userPw"
LOGIN_BUTTON_SELECTOR = "button.login-button"
CLOSE_BUTTON_SELECTOR = "button.layer-close-bt"

DOMAINS = {"m.nsmall.com"}
SITE_KEY = "nsmall"

# 주문당 상세 화면 1개를 여는 사이트. 기본 간격(1.5~4초)은 봇 확인이 잘 뜨는
# 사이트를 기준으로 잡은 값이라, 화면 하나 여는 데 1.2초쯤 걸리는 여기서는
# 조회 시간의 절반이 그냥 쉬는 시간이었다 (2026-09-04 실측: 롯데아이몰 6건
# 15.5초 중 순수 조회 7.5초). 네이버와 같은 간격으로 둔다 - 사람이 주문을
# 하나씩 눌러 보는 속도다.
REQUEST_GAP = (1.0, 2.0)


LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초
TRACKING_RESPONSE_TIMEOUT_MS = 10 * 1000  # 배송조회 클릭 후 API 응답 대기 최대 10초

TRACKING_LINK_TEXT = "배송조회"
TRACKING_API_MARKER = "order-dlvr-detail-with-gift"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문접수"]


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("orderNum")
    if not values:
        raise ParseError(f"URL에서 orderNum 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


SCREEN_ORDER = "order"   # 주문상세가 그려졌다 (주문번호가 화면에 있다)
SCREEN_LOGIN = "login"   # 로그인 화면이다 (비밀번호 입력창이 있다)
SCREEN_NONE = "none"     # 시간 안에 둘 다 안 나왔다


def _wait_for_order_or_login(page: Page, order_no: str, timeout_ms: int, poll_ms: int = 100) -> str:
    """주문상세가 그려지거나 로그인 화면이 뜨거나, 둘 중 먼저 오는 쪽을 알려준다.

    이 사이트는 서버가 302로 넘기지 않고 화면의 자바스크립트가 로그인 상태를
    확인한 뒤 로그인 화면으로 넘긴다. 실측(2026-09-03, 만료된 토큰): 주문상세
    주소로 들어가 0.7초에 토큰이 지워지고 1.2초에 로그인 폼이 그려진 뒤 1.3초에
    주소가 바뀐다. 예전에는 '주소가 로그인으로 바뀌는지 1.5초 지켜보기'로
    판정했는데, 두 가지가 문제였다.

    - 로그인이 살아 있는 평소에는 주소가 안 바뀌니 1.5초를 꼬박 기다렸다.
      주문상세는 0.3~0.6초면 다 그려지는데 매 주문 1초 남짓을 버린 셈이다.
    - 느린 날에 넘어가는 데 1.5초를 넘기면 '로그인 아님'으로 보고, 주문 정보가
      아직 없는 화면을 읽어 엉뚱한 사유("배송조회 버튼 없음")로 실패했다.
      2026-09-03 17:24 실행에서 '상품 준비 중'인 주문이 그렇게 기록됐다.

    그래서 주소 대신 화면을 본다 - 주문번호가 찍히면 주문상세이고, 비밀번호
    입력창이 생기면 로그인 화면이다. 어느 쪽이든 나타나는 즉시 끝난다.
    """
    waited_ms = 0
    while True:
        try:
            if order_no in page.inner_text("body"):
                return SCREEN_ORDER
        except Exception:  # noqa: BLE001 - 그리는 중에는 읽기가 실패할 수 있다
            pass
        if page.locator(LOGIN_PW_SELECTOR).count() > 0:
            return SCREEN_LOGIN
        if waited_ms >= timeout_ms:
            return SCREEN_NONE
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms


def _auto_login(page: Page) -> None:
    """NSMALL_ID/NSMALL_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    SSG/더현대 어댑터와 동일한 패턴 - NS홈쇼핑은 자동 클릭 로그인이 reCAPTCHA
    등에 막히지 않는 것을 확인했다. 로그인 버튼을 누른 뒤에는 로그인 폼이
    사라질 때까지만 기다린다(예전에는 1.5초 자고 주소를 1.5초 지켜보기를
    반복해 최소 3초가 걸렸다). 로그인이 되면 사이트가 joinRedirectUri의
    주문상세로 스스로 넘어간다.

    저장해 둔 세션으로 로그인이 유지되는 시간은 짧다 - refresh_token(JWT)의
    수명이 30분이라(2026-09-03 디코드), 지난 실행에서 30분이 넘게 지났으면
    첫 주문에서 반드시 이 경로를 탄다. 같은 실행 안의 나머지 주문은 새 토큰으로
    바로 조회된다.
    """
    login_id = os.environ.get("NSMALL_ID")
    login_pw = os.environ.get("NSMALL_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "NS홈쇼핑 로그인이 필요하지만 NSMALL_ID/NSMALL_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    page.click(LOGIN_BUTTON_SELECTOR)
    try:
        page.wait_for_selector(LOGIN_PW_SELECTOR, state="detached", timeout=LOGIN_WAIT_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001 - 시간 안에 로그인 화면을 못 벗어났다
        raise BlockedError("NS홈쇼핑 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.") from e


def _open_order_screen(page: Page, product_url: str, order_no: str) -> None:
    """주문상세 화면이 그려질 때까지 책임진다 - 로그인이 필요하면 하고,
    늦게 그려지면 한 번 다시 불러보고, 끝내 안 되면 그 사유로 실패시킨다.

    '주문번호가 화면에 있다'가 다 그려졌다는 유일한 기준이다. '배송조회' 같은
    글자는 상단 메뉴에도 있어서 표식으로 쓰면 덜 그려진 화면을 다 그려진 것으로
    볼 수 있고, 덜 그려진 화면을 읽으면 '아직 미발급'으로 조용히 틀릴 수 있다.
    """
    page.goto(product_url, wait_until="domcontentloaded")
    screen = _wait_for_order_or_login(page, order_no, common.RENDER_WAIT_TIMEOUT_MS)

    if screen == SCREEN_LOGIN:
        common.safe_print("[nsmall] 로그인 세션이 없어 자동 로그인을 시도합니다.")
        _auto_login(page)
        if parse_qs(urlparse(page.url).query).get("orderNum", [None])[0] != order_no:
            page.goto(product_url, wait_until="domcontentloaded")
        screen = _wait_for_order_or_login(page, order_no, common.RENDER_WAIT_TIMEOUT_MS)
        if screen == SCREEN_LOGIN:
            raise BlockedError("NS홈쇼핑 로그인 후에도 여전히 로그인 페이지입니다.")

    if screen == SCREEN_NONE:
        common.safe_print(f"[nsmall] 주문상세 화면이 늦게 그려져 다시 불러옵니다 (주문번호={order_no}).")
        page.goto(product_url, wait_until="domcontentloaded")
        screen = _wait_for_order_or_login(page, order_no, common.RENDER_WAIT_TIMEOUT_MS)

    if screen != SCREEN_ORDER:
        raise ParseError(
            f"주문상세 화면에 주문번호가 나타나지 않았습니다 (주문번호={order_no}, 현재 주소={page.url}) - "
            "화면이 그려지지 않았거나 다른 화면으로 넘어간 것으로 보입니다."
        )


def _parse_tracking_response(body: dict, order_no: str) -> tuple[str, str]:
    result_data = ((body.get("data") or {}).get("resultData")) or {}
    tracking_list = result_data.get("dlvrTrackingInfoList") or []
    order_items = result_data.get("orderItems") or {}

    wbl_num = (tracking_list[0].get("wblNum") if tracking_list else None) or order_items.get("wblNum")
    if not wbl_num:
        raise ParseError(f"배송조회 응답에서 송장번호(wblNum)를 찾지 못했습니다 (주문번호={order_no}).")
    tracking_no = re.sub(r"[^0-9]", "", wbl_num)

    courier_raw = (tracking_list[0].get("lscNm") if tracking_list else None) or order_items.get("dlvrEntCdNm")
    courier = common.normalize_courier(courier_raw.strip()) if courier_raw and courier_raw.strip() else "택배"

    return tracking_no, courier


def _click_tracking_link(page: Page, order_no: str, link) -> tuple[str, str]:
    with page.expect_response(lambda r: TRACKING_API_MARKER in r.url, timeout=TRACKING_RESPONSE_TIMEOUT_MS) as resp_info:
        link.click()
    result = _parse_tracking_response(resp_info.value.json(), order_no)

    close_button = page.locator(CLOSE_BUTTON_SELECTOR)
    if close_button.count() > 0:
        close_button.first.click()
        page.wait_for_timeout(500)

    return result


def _select_link_index_by_order_option(body_text: str, count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 "배송조회" 버튼 근처(상품명/옵션은 그 앞에
    나온다) 텍스트에만 유일하게 나타나면 그 버튼의 인덱스를 쓴다. 0개 또는 2개 이상
    매칭되면 None - 호출자가 전부 클릭해서 비교하는 방식으로 넘어간다."""
    if count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    positions = [m.start() for m in re.finditer(re.escape(TRACKING_LINK_TEXT), body_text)]
    if len(positions) != count:
        return None  # 텍스트와 버튼 개수가 안 맞으면(예상치 못한 구조) 안전하게 포기
    candidates = []
    prev_end = 0
    for idx, pos in enumerate(positions):
        window = body_text[max(prev_end, pos - 500) : pos]
        if target in normalize_option(window):
            candidates.append(idx)
        prev_end = pos
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page: Page, order_no: str, order_option: str | None) -> TrackingResult:
    links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
    count = links.count()

    if count == 0:
        body_text = page.inner_text("body")
        normalized_body = normalize_option(body_text)
        if any(normalize_option(p) in normalized_body for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"화면에서 배송조회 버튼을 찾지 못했습니다 (주문번호={order_no}).")

    if count == 1:
        tracking_no, courier = _click_tracking_link(page, order_no, links.first)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    body_text = page.inner_text("body")
    matched_idx = _select_link_index_by_order_option(body_text, count, order_option)
    if matched_idx is not None:
        tracking_no, courier = _click_tracking_link(page, order_no, links.nth(matched_idx))
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 클릭해서 실제로 서로 다른 송장인지 확인한다
    # (다른 어댑터와 동일한 안전 규칙). 클릭할 때마다 모달을 닫으므로 매번 버튼을
    # 새로 조회해야 한다(닫기 애니메이션 등으로 이전 로케이터가 불안정할 수 있다).
    results = []
    for i in range(count):
        fresh_links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
        results.append(_click_tracking_link(page, order_no, fresh_links.nth(i)))

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
        _open_order_screen(page, product_url, order_no)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        page.close()
