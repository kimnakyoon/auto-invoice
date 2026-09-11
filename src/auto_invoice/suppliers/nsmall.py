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
- 1:1 문의 남기기(post_inquiry)는 아래 '1:1 문의 남기기' 구간에 실측을 적어뒀다 (2026-09-09).
- **주문목록 API 한 번으로 여러 건 답하기 (prepare_batch, 2026-09-11 실측).** 화면 경로는
  주문마다 화면을 열고 [배송조회]를 눌러 건당 1~3초에 요청 간격(REQUEST_GAP 1~2초)까지
  더해져 4~5초였다(최근 다섯 실행 모두). 문의 등록이 쓰는 mapi(Bearer accessToken)에
  주문목록 API가 있다 - 문의 레이어의 [상품 선택]이 부르는 GET
  order/order/order-list?pageNum=&pageSize=&orderDttm1=&orderDttm2=&inqrCond=list&reqSpr=mobile
  &stat=&goodsNm=&totalPage= (날짜는 YYYY-MM-DD, 화면 기본은 최근 1개월·10건씩인데 pageSize=50도
  받고 0.13초). 응답 resultData.orders[]마다 orderNum·orderDttm과 orderItems[](상품별
  wblNum(송장)·dlvrEntCd(택배사 코드)·orderRtnClssfCdNm(주문/취소)·reltStatCdNm(출고지시/
  출고완료/배송완료/출고지시후취소)·unitNm(옵션)·dlvrSchdDttm(도착예정일)·배송조회 API
  파라미터(custDstnClssfNum/orderRtnClssfCd/reltStatCd/goodsCd/unitCd))가 있어 화면 없이
  결론이 난다. 택배사 **이름**은 목록에 코드뿐이라, 송장이 있는 건만 배송조회 API
  (order-dlvr-detail-with-gift - 화면의 [배송조회]가 부르는 그 요청, unitCd까지 다섯 파라미터가
  전부 있어야 하고 하나라도 빠지면 400)를 한 번 불러 lscNm을 읽고 코드별로 기억한다
  (실측 코드 20=롯데택배). 세션(accessToken)은 문의와 같은 _inquiry_session으로 주문상세
  화면을 한 번 열어 읽는다(0.6초, 로그인 필요하면 자동 로그인). 목록에 없는 주문(1개월보다
  오래됨)과 목록을 못 읽은 경우만 예전 화면 경로로 간다.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import eta as eta_mod
from ..models import TrackingResult
from . import common
from .base import (
    AdapterError,
    AlreadyInquired,
    BlockedError,
    OrderCancelled,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    find_cancelled_keyword,
    normalize_option,
    raise_if_cancelled,
    raise_if_cancelled_any,
    raise_if_delayed_any,
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

    # 주문목록 API로 이미 읽어둔 주문이면 화면을 열지 않고 여기서 끝낸다
    # (prepare_batch - 파일 맨 아래 구간). 목록에 없던 주문만 화면으로.
    listed = _listed_orders.get(id(context), {}).get(order_no)
    if listed is not None:
        answered = _answer_from_list(context, product_url, listed, order_no, order_option)
        if answered is not None:
            return answered

    page = context.new_page()
    try:
        _open_order_screen(page, product_url, order_no)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        page.close()


# ---------------------------------------------------------------------------
# 1:1 문의 남기기 (post_inquiry) - 2026-09-09 실측
#
# 사용자 지시 순서: 주문상세에서 주문번호를 복사 → 오른쪽 메뉴 [고객센터] →
# 가운데 [1:1문의] → 문의 유형 [배송·수거] → 문의유형 [배송문의] → 주문번호로 상품
# 선택 → 제목·내용 "수취인명 배송 언제 시작하나요?" → [문의하기] → 오른쪽 메뉴
# [상담내역 > 1:1 문의]에서 등록 확인.
#
# 실측:
# - 고객센터는 고정 주소 m.nsmall.com/customer-center. 가운데 [1:1문의](a.inquiry-btn)는
#   주소 이동 없이 같은 페이지 위에 레이어(div.modal-inquiry-privacy)를 띄운다.
#   문의 유형은 체크박스(label > span "배송·수거") → GET customercenter/qst-cscate?largeCaCd=2
#   로 중분류 [배송문의(31)·수거문의(749)]가 오고, 커스텀 드롭다운(.dropdown-wrap
#   button.result-item → button.contents-item)에서 [배송문의]를 고르면 소분류
#   (qst-cscate?mediumCaCd=31)는 [배송일(시간)문의(485)] 하나뿐이라 자동으로 잡힌다
#   (숨은 input[name=custom-select-01]=31, custom-select-02=485).
# - [상품 선택](button.goods-select-btn)은 두 번째 레이어(div.modal-inquiry-goods-select)에
#   주문목록 API(order/order-list, 최근 1개월, 10건씩·아래 페이지 번호 버튼 ul.pagination-wrap)를
#   그린다 - 주문마다 div.goods-status-wrap(span.order-number b = 주문번호, 상품마다 li와 button.choice-btn).
# - 제목 #title(최대 25자)·내용 textarea(최대 200자, 최소 4자)·SMS 답변 알림(#sms, 기본
#   체크, 전화번호 #inputField는 회원 정보로 미리 채워짐) → [문의하기](.layer-bottom
#   button.button, 채우기 전엔 disabled).
# - 등록 = POST mapi.nsmall.com/or/api/v1/cust/customercenter/qst (JSON, Bearer
#   accessToken). 사이트 JS(modal-inquiry-privacy 컴포넌트)가 만드는 본문:
#   {title, ctnt, custNm: userStore.custInfo.custNm, boardClssfCd:"Q", confGb: SMS면 "01",
#    largeCaCd:"2", mediumCaCd:"31", smallCaCd:"485", orderNum: Number, orderSeq: 고른
#    상품의 maxOrderSeq, mobilDdd/mobilHtel/mobilNum: 전화번호를 '-'로 나눈 셋}.
#   응답 {"data":{"resultCode":"0000",...}} → 레이어가 닫히고 상담내역 목록으로 간다
#   (alert/confirm 없음).
# - 상담내역 [1:1 문의] = GET customercenter/qst?pageNum=N&pageSize=10 (최신순,
#   custCmplnNum·goodsCd·goodsNm·largeCaCd "배송"·ansrYn·qstDate "2026.09.08"·title).
#   상세 POST customercenter/qst-dtl {custCmplnNum} 에도 주문번호는 없다 - 상품코드
#   (goodsCd)와 제목·문의일로 이 주문의 문의인지 맞춘다.
# - 세션: accessToken(JWT, 15분)은 sessionStorage 'access_token'에, 회원 이름·전화는
#   Vue 3 Pinia 저장소 userStore.custInfo(custNm, phoneNum - API 응답은 [ENC] 암호문인데
#   화면이 복호화해 둔다)에 있다. 화면을 한 번 열어(로그인 포함) 읽어두고 그 값으로
#   주문상세·상담내역·등록을 전부 요청으로 처리한다 - 화면 경로는 폴백.
# ---------------------------------------------------------------------------
INQUIRY_API_BASE = "https://mapi.nsmall.com/or/api/v1"
INQUIRY_POST_URL = INQUIRY_API_BASE + "/cust/customercenter/qst"
INQUIRY_LIST_URL = INQUIRY_API_BASE + "/cust/customercenter/qst?pageNum={page}&pageSize={size}"
INQUIRY_TYPE_LIST_URL = INQUIRY_API_BASE + "/cust/customercenter/qst-cscate?largeCaCd={large}"
INQUIRY_SUBTYPE_LIST_URL = INQUIRY_API_BASE + "/cust/customercenter/qst-cscate?mediumCaCd={medium}"
ORDER_DETAIL_API_URL = INQUIRY_API_BASE + "/order/order/order-detail?orderNum={order_no}&reqSpr=mobile"
INQUIRY_LIST_PAGE_SIZE = 10
INQUIRY_TYPE_LARGE = "2"              # 문의 유형 [배송·수거]
INQUIRY_TYPE_LARGE_LABEL = "배송·수거"
INQUIRY_TYPE_MEDIUM = "31"            # 문의유형 [배송문의]
INQUIRY_TYPE_MEDIUM_LABEL = "배송문의"
INQUIRY_TYPE_SMALL = "485"            # 소분류 [배송일(시간)문의] - 배송문의의 유일한 소분류라 자동 선택
INQUIRY_TYPE_SMALL_LABEL = "배송일(시간)문의"
INQUIRY_LIST_CATEGORY = "배송"        # 상담내역 largeCaCd에 찍히는 [배송·수거]의 이름
INQUIRY_TITLE_MAX = 25
INQUIRY_CONTENT_MAX = 200
INQUIRY_SAME_MARK = "배송 언제"       # 상담내역에서 '같은 문의'로 보는 표식 (상품코드와 함께) - 사람이 남긴 것용
INQUIRY_TITLE_MIN = 4                 # 사이트의 최소 글자 수 - 이보다 짧은 제목은 문구 앞부분으로 치지 않는다
INQUIRY_HISTORY_TRIES = 3             # 등록 뒤 목록에 아직 안 보이면 이만큼 다시 본다
INQUIRY_HISTORY_RETRY_GAP_SEC = 1.0
INQUIRY_HISTORY_MAX_PAGES = 5         # '이미 남겼는지' 훑는 상담내역 페이지 수
INQUIRY_TOKEN_MARGIN_SEC = 30         # accessToken 만료가 이보다 가까우면 화면을 다시 열어 받는다
# 화면의 axios가 mapi 요청마다 붙이는 헤더 (2026-09-09 가로채기로 확인). 읽기 GET은 authorization만
# 있어도 200이지만, 등록 POST를 accpt-path-cd·ptn-cd 없이 보냈더니 400이었다(그날 첫 실등록은 화면
# 경로로 남김). 둘을 붙이니 통과 - 같은 날 두 번째 실등록(560908003321 → 문의번호 260909011483, 0.17초).
INQUIRY_API_HEADERS = {"origin": "https://m.nsmall.com", "referer": "https://m.nsmall.com/",
                       "accept": "application/json, text/plain, */*",
                       "accpt-path-cd": "100", "ptn-cd": "110"}
INQUIRY_POST_CONTENT_TYPE = "application/json;charset=UTF-8"

CUSTOMER_CENTER_URL = "https://m.nsmall.com/customer-center"
INQUIRY_OPEN_BUTTON = "a.inquiry-btn"
INQUIRY_MODAL = "div.modal-inquiry-privacy"
INQUIRY_GOODS_MODAL = "div.modal-inquiry-goods-select"
INQUIRY_TYPE_DROPDOWN = INQUIRY_MODAL + " .dropdown-wrap"
INQUIRY_TYPE_MEDIUM_INPUT = INQUIRY_MODAL + " input[name=custom-select-01]"
INQUIRY_TYPE_SMALL_INPUT = INQUIRY_MODAL + " input[name=custom-select-02]"
INQUIRY_GOODS_BUTTON = INQUIRY_MODAL + " button.goods-select-btn"
INQUIRY_GOODS_BLOCK = INQUIRY_GOODS_MODAL + " .goods-status-wrap"
INQUIRY_GOODS_PAGING = INQUIRY_GOODS_MODAL + " .pagination-wrap"
INQUIRY_GOODS_LIST_API = "/order/order/order-list?"
INQUIRY_GOODS_CHOSEN_NAME = INQUIRY_MODAL + " .goods-inquiry-wrap .goods-name"
INQUIRY_TITLE = INQUIRY_MODAL + " #title"
INQUIRY_CONTENT = INQUIRY_MODAL + " textarea"
INQUIRY_SUBMIT = INQUIRY_MODAL + " .layer-bottom button.button"
INQUIRY_STEP_WAIT_MS = 10000          # 레이어·드롭다운·상품목록·등록 응답까지 최대
INQUIRY_GOODS_PAGE_MAX = 10           # 상품 선택 레이어에서 주문을 찾을 때까지 넘겨 보는 페이지 수 (10건씩)
INQUIRY_ALLOWED_HOSTS = ("nsmall.com",)

# 이 실행(컨텍스트)에서 화면을 열어 읽어둔 세션 {"bearer", "cust_nm", "phone", "expires"}
# 와 상담내역 캐시 {"rows": [...최신순], "pages": n, "total": n}. prepare_inquiries가 비운다.
_inquiry_session_cache: dict[int, dict] = {}
_inquiry_rows_cache: dict[int, dict] = {}
# 유형 코드 확인은 배치에 한 번이면 된다 (유형 목록 두 번 = 0.06초/건).
_inquiry_types_checked: set[int] = set()

READ_SESSION_JS = """() => {
  const app = document.querySelector('#app') || document.body.firstElementChild;
  const vapp = app && app.__vue_app__;
  const pinia = vapp && vapp.config.globalProperties.$pinia;
  const store = pinia && pinia._s.get('userStore');
  const cust = (store && store.$state && store.$state.custInfo) || {};
  return {token: sessionStorage.getItem('access_token'), cust_nm: cust.custNm || null, phone: cust.phoneNum || null};
}"""


def prepare_inquiries(context: BrowserContext, product_urls, headless: bool = False) -> None:
    """새 배치 - 앞 배치에서 읽어둔 세션·상담내역 캐시를 비운다."""
    _inquiry_session_cache.pop(id(context), None)
    _inquiry_rows_cache.pop(id(context), None)
    _inquiry_types_checked.discard(id(context))


def _token_expiry(token: str) -> float:
    """JWT의 exp(초, epoch). 못 읽으면 0 - 만료로 친다."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:  # noqa: BLE001 - 형식이 다르면 만료로 보고 다시 받는다
        return 0.0


def _format_phone(digits: str) -> str:
    """01022178032 -> 010-2217-8032 (화면의 답변 알림 칸이 이렇게 채운다)."""
    digits = re.sub(r"[^0-9]", "", digits or "")
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


SESSION_OR_LOGIN_JS = """([pw]) => {
  const app = document.querySelector('#app') || document.body.firstElementChild;
  const vapp = app && app.__vue_app__;
  const pinia = vapp && vapp.config.globalProperties.$pinia;
  const store = pinia && pinia._s.get('userStore');
  const cust = (store && store.$state && store.$state.custInfo) || {};
  return (!!sessionStorage.getItem('access_token') && !!cust.custNm) || !!document.querySelector(pw);
}"""


def _wait_for_session(page: Page, product_url: str, order_no: str, timeout_ms: int) -> dict:
    """주문상세 주소를 열고 토큰·회원 이름이 생기는 즉시 읽는다 - 주문번호가 그려질 때까지 기다리지 않는다.

    실측(2026-09-09): 토큰·회원정보는 0.47~0.61초, 주문번호 렌더는 0.70~0.99초 - 화면 안에서
    도는 wait_for_function으로 기다리면 세션 읽기가 0.71초→0.58초(4회 중앙값). 로그인 화면
    (비밀번호 칸)이 먼저 뜨면 자동 로그인하고 한 번 더 기다린다 - 로그인되면 사이트가
    주문상세로 스스로 돌아오며 새 토큰을 받는다.
    """
    page.goto(product_url, wait_until="domcontentloaded")
    for attempt in range(2):
        try:
            page.wait_for_function(SESSION_OR_LOGIN_JS, arg=[LOGIN_PW_SELECTOR], timeout=timeout_ms)
        except PlaywrightTimeoutError:
            raise BlockedError(f"NS홈쇼핑 화면에서 로그인 토큰(access_token)을 읽지 못했습니다 "
                               f"(주문번호={order_no}, 현재 주소={page.url}).") from None
        found = page.evaluate(READ_SESSION_JS)
        token = found.get("token") or ""
        if token and found.get("cust_nm") and _token_expiry(token) - time.time() > INQUIRY_TOKEN_MARGIN_SEC:
            return found
        if attempt == 0 and page.locator(LOGIN_PW_SELECTOR).count() > 0:
            common.safe_print("[nsmall] 로그인 세션이 없어 자동 로그인을 시도합니다.")
            _auto_login(page)
            continue
        break
    raise ParseError("NS홈쇼핑 화면에서 회원 이름(userStore.custInfo)이나 살아 있는 토큰을 읽지 못했습니다 - 문의 등록에 필요합니다.")


def _read_session(context: BrowserContext, product_url: str, order_no: str) -> dict:
    """주문상세 화면을 열어(로그인이 필요하면 하고) accessToken·회원 이름·전화를 읽는다."""
    page = context.new_page()
    page.route("**/*", _guard_inquiry_page)   # 분석·광고 스크립트를 끊으면 화면이 더 빨리 뜬다
    try:
        found = _wait_for_session(page, product_url, order_no, common.RENDER_WAIT_TIMEOUT_MS)
    finally:
        page.close()
    return {"bearer": f"Bearer {found['token']}", "cust_nm": found["cust_nm"],
            "phone": _format_phone(found.get("phone") or ""), "expires": _token_expiry(found["token"])}


def _inquiry_session(context: BrowserContext, product_url: str, order_no: str, *,
                     refresh: bool = False) -> dict:
    """읽어둔 세션. 없거나 토큰이 곧 만료(15분짜리)면 화면을 다시 열어 받는다."""
    cached = _inquiry_session_cache.get(id(context))
    if refresh or cached is None or cached["expires"] - time.time() < INQUIRY_TOKEN_MARGIN_SEC:
        cached = _read_session(context, product_url, order_no)
        _inquiry_session_cache[id(context)] = cached
    return cached


def _api(context: BrowserContext, session: dict, url: str, *, data: dict | None = None):
    """mapi 요청 한 번 - 정상이면 resultData, 401이면 None(토큰 만료), 그 밖의 오류는 ParseError."""
    headers = {"authorization": session["bearer"], **INQUIRY_API_HEADERS}
    if data is not None:
        headers["content-type"] = INQUIRY_POST_CONTENT_TYPE
    short = url.split("/v1/")[-1]
    try:
        response = (context.request.post(url, data=data, headers=headers) if data is not None
                    else context.request.get(url, headers=headers))
    except Exception as e:  # noqa: BLE001 - 통신 실패
        raise ParseError(f"NS홈쇼핑 요청에 실패했습니다 ({short}: {e}).") from None
    if response.status == 401:
        return None
    if not response.ok:
        raise ParseError(f"NS홈쇼핑 요청이 거부됐습니다 (HTTP {response.status}, {short}).")
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - JSON이 아니면 화면이 바뀐 것
        raise ParseError(f"NS홈쇼핑 응답이 JSON이 아닙니다 ({short}).") from None
    data_part = (body or {}).get("data") or {}
    if str(data_part.get("resultCode")) != "0000":
        raise ParseError(f"NS홈쇼핑이 요청을 거절했습니다 ({short}: "
                         f"{data_part.get('resultCode')} {data_part.get('resultMessage')}).")
    return data_part.get("resultData")


def _api_logged_in(context: BrowserContext, product_url: str, order_no: str, url: str, *,
                   data: dict | None = None):
    """_api에 토큰 만료(401)면 화면을 다시 열어 로그인·토큰을 새로 받고 한 번 더."""
    session = _inquiry_session(context, product_url, order_no)
    result = _api(context, session, url, data=data)
    if result is None:
        common.safe_print("[nsmall] 로그인 토큰이 만료돼 화면을 다시 열어 받습니다.")
        session = _inquiry_session(context, product_url, order_no, refresh=True)
        result = _api(context, session, url, data=data)
        if result is None:
            raise BlockedError("NS홈쇼핑 로그인 토큰을 새로 받았는데도 요청이 401입니다.")
    return result


def _order_for_inquiry(context: BrowserContext, product_url: str, order_no: str) -> dict:
    """주문상세 API에서 문의할 상품(첫 상품)의 코드·이름·순번을 읽고 취소/품절이면 올린다.

    상태는 상품 줄의 orderRtnClssfCdNm(주문/취소...)·reltStatCdNm(출고지시/출고완료...)만
    본다. '준비 중'(TrackingNotAvailableYet)은 문의 대상 그 자체라 지나간다.
    """
    detail = _api_logged_in(context, product_url, order_no, ORDER_DETAIL_API_URL.format(order_no=order_no)) or {}
    orders = detail.get("orders") or {}
    if str(orders.get("orderNum") or "") != order_no:
        raise ParseError(f"주문상세가 열리지 않았습니다 (주문번호={order_no}).")
    items = [it for ship in (orders.get("ships") or [])
             for it in ((ship.get("dlvrOrderItems") or []) + (ship.get("pickOrderItems") or []))
             if str(it.get("orderNum") or order_no) == order_no]
    if not items:
        raise ParseError(f"주문상세에 상품이 없습니다 (주문번호={order_no}).")
    statuses = [f"{it.get('orderRtnClssfCdNm') or ''} {it.get('reltStatCdNm') or ''}".strip() for it in items]
    with contextlib.suppress(TrackingNotAvailableYet):
        raise_if_cancelled_any(statuses, order_no)
    first = items[0]
    if not first.get("goodsCd"):
        raise ParseError(f"주문상세 상품에 상품코드(goodsCd)가 없습니다 (주문번호={order_no}).")
    try:   # 주문번호에는 연도가 없어(560907010024) 주문일은 orderDttm("20260907")에서
        order_date = datetime.strptime(str(orders.get("orderDttm") or "")[:8], "%Y%m%d").date()
    except ValueError:
        order_date = None
    return {"order_no": order_no, "goods_cd": str(first["goodsCd"]), "goods_nm": str(first.get("goodsNm") or "").strip(),
            "order_seq": int(first.get("maxOrderSeq") or 1), "statuses": statuses, "order_date": order_date}


def _parse_inquiry_rows(items: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for it in items or []:
        try:
            written = datetime.strptime(str(it.get("qstDate") or ""), "%Y.%m.%d").date()
        except ValueError:
            continue
        rows.append({
            "inquiry_id": str(it.get("custCmplnNum") or ""),
            "goods_cd": str(it.get("goodsCd") or ""),
            "category": str(it.get("largeCaCd") or "").strip(),
            "title": str(it.get("title") or "").strip(),
            "written_on": written,
            "state": "답변완료" if str(it.get("ansrYn")) == "Y" else "답변대기",
        })
    return rows


def _fetch_inquiry_page(context: BrowserContext, product_url: str, order_no: str, page_no: int) -> tuple[list[dict], int]:
    """상담내역 한 페이지 (줄들, 전체 건수). 못 읽으면 ParseError - 모르는 채로 등록하지 않는다."""
    result = _api_logged_in(context, product_url, order_no,
                            INQUIRY_LIST_URL.format(page=page_no, size=INQUIRY_LIST_PAGE_SIZE))
    if not isinstance(result, dict) or "list" not in result:
        raise ParseError("상담내역을 읽지 못했습니다 (응답 모양이 다릅니다).")
    return _parse_inquiry_rows(result.get("list") or []), int(result.get("total") or 0)


def _load_inquiry_rows(context: BrowserContext, product_url: str, order_no: str, since: date | None, *,
                       refresh: bool = False, max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> list[dict]:
    """since 이후 줄이 다 들어올 때까지 상담내역을 읽어둔 캐시(최신순). refresh면 1페이지부터 새로."""
    cache = _inquiry_rows_cache.get(id(context))
    if cache is None or refresh:
        rows, total = _fetch_inquiry_page(context, product_url, order_no, 1)
        cache = {"rows": rows, "pages": 1, "total": total}
        _inquiry_rows_cache[id(context)] = cache
    while (since is not None and cache["rows"] and cache["pages"] < max_pages
           and len(cache["rows"]) < cache["total"] and cache["rows"][-1]["written_on"] >= since):
        more, _ = _fetch_inquiry_page(context, product_url, order_no, cache["pages"] + 1)
        if not more:
            break
        cache["rows"].extend(more)
        cache["pages"] += 1
    return cache["rows"]


def _describe_listed(entry: dict) -> str:
    return (f"{entry['state']} {entry['written_on']:%Y.%m.%d} "
            f"(문의번호 {entry['inquiry_id']}, [{entry['category']}] '{entry['title']}')")


def _same_inquiry(entry: dict, order: dict, message: str) -> bool:
    """이 상품의 [배송] 문의이고, 제목이 우리 문구의 앞부분(제목은 25자에서 잘린다 - 2026-09-09 실등록
    'CHI MICHAEL CHRISTOPHER 배')이거나 사람이 남긴 '배송 언제' 문의면 같은 문의로 본다."""
    title = entry["title"]
    return (entry["goods_cd"] == order["goods_cd"] and entry["category"] == INQUIRY_LIST_CATEGORY
            and (INQUIRY_SAME_MARK in title
                 or (len(title) >= INQUIRY_TITLE_MIN and message.startswith(title))))


def _find_listed_inquiry(context: BrowserContext, product_url: str, order: dict, message: str, since: date | None, *,
                         refresh: bool = False, max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> dict | None:
    """상담내역에서 since 이후에 쓴, 이 상품의 같은 문의(_same_inquiry)를 찾는다 (사람이 직접 남긴 것 포함).

    상담내역에는 주문번호가 없어 상품코드로 맞춘다 - 같은 상품을 며칠 사이에 두 번 주문했으면
    앞 주문의 문의가 뒤 주문 것으로 보일 수 있다(그때는 넘김으로 적히니 결과 엑셀에서 확인).
    """
    for entry in _load_inquiry_rows(context, product_url, order["order_no"], since, refresh=refresh, max_pages=max_pages):
        if since is not None and entry["written_on"] < since:
            return None   # 최신순이라 여기부터는 전부 더 오래된 것
        if _same_inquiry(entry, order, message):
            return entry
    return None


def _confirm_inquiry_listed(context: BrowserContext, product_url: str, order: dict, message: str) -> str:
    """등록 뒤 상담내역을 새로 받아 오늘 자로 올라갔는지 본다. 목록이 늦게 갱신될 수 있어 몇 번 다시 본다."""
    today = date.today()
    for attempt in range(1, INQUIRY_HISTORY_TRIES + 1):
        found = _find_listed_inquiry(context, product_url, order, message, today, refresh=True, max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < INQUIRY_HISTORY_TRIES:
            common.sleep(INQUIRY_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"[문의하기]는 보냈지만 상담내역에서 확인되지 않았습니다. "
        f"다시 남기기 전에 NS홈쇼핑 고객센터 > 상담내역 > 1:1 문의에서 '{message}'가 있는지 직접 확인해주세요.")


def _inquiry_payload(session: dict, order: dict, message: str) -> dict:
    """사람이 [문의하기]를 눌렀을 때 화면 JS가 보내는 본문과 같은 모양 (SMS 답변 알림 켬)."""
    ddd, htel, num = (session["phone"].split("-") + ["", "", ""])[:3]
    return {
        "title": message[:INQUIRY_TITLE_MAX], "ctnt": message[:INQUIRY_CONTENT_MAX],
        "custNm": session["cust_nm"], "boardClssfCd": "Q", "confGb": "01",
        "largeCaCd": INQUIRY_TYPE_LARGE, "mediumCaCd": INQUIRY_TYPE_MEDIUM, "smallCaCd": INQUIRY_TYPE_SMALL,
        "orderNum": int(order["order_no"]), "orderSeq": order["order_seq"],
        "mobilDdd": ddd, "mobilHtel": htel, "mobilNum": num,
    }


def _check_inquiry_types(context: BrowserContext, product_url: str, order_no: str) -> None:
    """유형 코드가 여전히 [배송·수거]>[배송문의]>[배송일(시간)문의]인지 유형 목록 API로 확인한다 (배치에 한 번)."""
    if id(context) in _inquiry_types_checked:
        return
    mediums = _api_logged_in(context, product_url, order_no, INQUIRY_TYPE_LIST_URL.format(large=INQUIRY_TYPE_LARGE)) or []
    medium = next((m for m in mediums if str(m.get("csClssfNum")) == INQUIRY_TYPE_MEDIUM), None)
    if medium is None or str(medium.get("csClssfNm") or "").strip() != INQUIRY_TYPE_MEDIUM_LABEL:
        raise ParseError(f"[{INQUIRY_TYPE_LARGE_LABEL}]의 문의유형에 [{INQUIRY_TYPE_MEDIUM_LABEL}]({INQUIRY_TYPE_MEDIUM})이 없습니다 "
                         f"(목록: {[m.get('csClssfNm') for m in mediums]}).")
    smalls = _api_logged_in(context, product_url, order_no, INQUIRY_SUBTYPE_LIST_URL.format(medium=INQUIRY_TYPE_MEDIUM)) or []
    small = next((s for s in smalls if str(s.get("csClssfNum")) == INQUIRY_TYPE_SMALL), None)
    if small is None or str(small.get("csClssfNm") or "").strip() != INQUIRY_TYPE_SMALL_LABEL:
        raise ParseError(f"[{INQUIRY_TYPE_MEDIUM_LABEL}]의 소분류에 [{INQUIRY_TYPE_SMALL_LABEL}]({INQUIRY_TYPE_SMALL})이 없습니다 "
                         f"(목록: {[s.get('csClssfNm') for s in smalls]}).")
    _inquiry_types_checked.add(id(context))


def _submit_via_api(context: BrowserContext, product_url: str, order: dict, message: str) -> str | None:
    """화면 JS가 보내는 등록 POST를 화면 없이 바로 보낸다 (네이버·GSSHOP·롯데아이몰과 같은 직행).

    resultCode 0000이면 성공 문구, HTTP 오류(401은 토큰을 새로 받아 한 번 더)면 None - 그때만
    화면 경로로 간다. 사이트가 내용을 거절(resultCode≠0000)하면 화면으로 보내도 같으니 ParseError.
    """
    _check_inquiry_types(context, product_url, order["order_no"])
    session = _inquiry_session(context, product_url, order["order_no"])
    payload = _inquiry_payload(session, order, message)
    try:
        result = _api_logged_in(context, product_url, order["order_no"], INQUIRY_POST_URL, data=payload)
    except ParseError as e:
        if "HTTP" not in str(e):
            raise
        common.safe_print(f"[nsmall] 등록 요청이 거부돼 화면으로 남깁니다 ({e}).")
        return None
    # resultData는 새 문의번호(custCmplnNum)다 - 2026-09-09 실등록 260909011483로 확인.
    return f"등록 요청 보냄 (직행, resultCode 0000{', 문의번호 ' + str(result)[:20] if result else ''})"


def _guard_inquiry_page(route) -> None:
    """문의 화면 전용 라우팅 - NS홈쇼핑 밖 호스트(분석·광고 스크립트)는 끊어 화면을 가볍게 한다."""
    host = urlparse(route.request.url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in INQUIRY_ALLOWED_HOSTS):
        route.continue_()
    else:
        route.abort()


def _pick_goods_in_layer(page, order: dict) -> None:
    """[상품 선택] 레이어에서 이 주문번호 묶음의 상품 [선택]을 누른다 - 없으면 다음 페이지(10건씩)로 넘기며 찾는다."""
    page.locator(INQUIRY_GOODS_BUTTON).click()
    page.locator(INQUIRY_GOODS_BLOCK).first.wait_for(timeout=INQUIRY_STEP_WAIT_MS)
    block = page.locator(INQUIRY_GOODS_BLOCK).filter(
        has=page.locator("span.order-number b", has_text=re.compile(rf"^\s*{re.escape(order['order_no'])}\s*$")))
    for _ in range(INQUIRY_GOODS_PAGE_MAX):
        if block.count() > 0:
            break
        active = page.locator(INQUIRY_GOODS_PAGING + " button.active")
        if active.count() == 0:
            break
        next_no = str(int(active.first.inner_text().strip()) + 1)
        next_button = page.locator(INQUIRY_GOODS_PAGING + " button", has_text=re.compile(rf"^\s*{next_no}\s*$"))
        if next_button.count() == 0:
            next_button = page.locator(INQUIRY_GOODS_PAGING + " button.next-btn:visible")
        if next_button.count() == 0:
            break   # 마지막 페이지
        with page.expect_response(lambda r, n=next_no: INQUIRY_GOODS_LIST_API in r.url and f"pageNum={n}&" in r.url,
                                  timeout=INQUIRY_STEP_WAIT_MS):
            next_button.first.click()
        page.locator(INQUIRY_GOODS_PAGING + " button.active", has_text=next_no).wait_for(timeout=INQUIRY_STEP_WAIT_MS)
    if block.count() == 0:
        raise ParseError(f"[상품 선택] 목록(최근 1개월)에 이 주문이 없습니다 (주문번호={order['order_no']}).")
    items = block.first.locator("li")
    chosen = items.filter(has_text=order["goods_nm"]) if order["goods_nm"] and items.count() > 1 else items
    if chosen.count() == 0:
        chosen = items
    chosen.first.locator("button.choice-btn").click()
    page.locator(INQUIRY_GOODS_MODAL).wait_for(state="detached", timeout=INQUIRY_STEP_WAIT_MS)
    shown = page.locator(INQUIRY_GOODS_CHOSEN_NAME).first.inner_text().strip()
    if order["goods_nm"] and shown != order["goods_nm"]:
        raise ParseError(f"문의 상품이 주문상세의 상품과 다릅니다 (화면 '{shown}', 주문 '{order['goods_nm']}').")


def _fill_inquiry_form(page, order: dict, message: str) -> None:
    """레이어에서 유형·상품·제목·내용을 채운다 - [문의하기]는 누르지 않는다."""
    modal = page.locator(INQUIRY_MODAL)
    modal.get_by_text(INQUIRY_TYPE_LARGE_LABEL, exact=True).click()
    dropdown = page.locator(INQUIRY_TYPE_DROPDOWN).first
    dropdown.locator("button.result-item").wait_for(timeout=INQUIRY_STEP_WAIT_MS)
    dropdown.locator("button.result-item").click()
    option = dropdown.locator("button.contents-item", has_text=INQUIRY_TYPE_MEDIUM_LABEL)
    try:
        option.first.wait_for(timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        seen = dropdown.locator("button.contents-item").all_inner_texts()
        raise ParseError(f"[{INQUIRY_TYPE_LARGE_LABEL}]을 골랐는데 문의유형에 [{INQUIRY_TYPE_MEDIUM_LABEL}]이 없습니다 (화면: {seen}).") from None
    option.first.click()
    try:
        page.wait_for_function(
            "([m, s, mv, sv]) => document.querySelector(m)?.value === mv && document.querySelector(s)?.value === sv",
            arg=[INQUIRY_TYPE_MEDIUM_INPUT, INQUIRY_TYPE_SMALL_INPUT, INQUIRY_TYPE_MEDIUM, INQUIRY_TYPE_SMALL],
            timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        chosen = (page.locator(INQUIRY_TYPE_MEDIUM_INPUT).input_value(), page.locator(INQUIRY_TYPE_SMALL_INPUT).input_value())
        raise ParseError(f"문의 유형이 {INQUIRY_TYPE_MEDIUM_LABEL}/{INQUIRY_TYPE_SMALL_LABEL}"
                         f"({INQUIRY_TYPE_MEDIUM}/{INQUIRY_TYPE_SMALL})로 잡히지 않았습니다 (화면: {chosen}).") from None
    _pick_goods_in_layer(page, order)
    page.fill(INQUIRY_TITLE, message[:INQUIRY_TITLE_MAX])
    page.fill(INQUIRY_CONTENT, message[:INQUIRY_CONTENT_MAX])
    if page.locator(INQUIRY_TITLE).input_value().strip() != message[:INQUIRY_TITLE_MAX].strip():
        raise ParseError("문의 제목이 입력되지 않았습니다.")
    if page.locator(INQUIRY_CONTENT).input_value().strip() != message[:INQUIRY_CONTENT_MAX]:
        raise ParseError("문의 내용이 입력되지 않았습니다.")


def _submit_via_form(context: BrowserContext, product_url: str, order: dict, message: str) -> str:
    """고객센터 화면을 열어 [1:1문의] 레이어를 채우고 [문의하기]를 눌러 등록 응답까지 본다."""
    order_no = order["order_no"]
    page = context.new_page()
    page.route("**/*", _guard_inquiry_page)
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(f"{d.type}: {d.message}"), d.accept()))
    try:
        _open_order_screen(page, product_url, order_no)   # 로그인 확인 겸 (고객센터는 로그인 화면으로 안 튕긴다)
        page.goto(CUSTOMER_CENTER_URL, wait_until="domcontentloaded")
        page.locator(INQUIRY_OPEN_BUTTON).first.wait_for(timeout=INQUIRY_STEP_WAIT_MS)
        page.locator(INQUIRY_OPEN_BUTTON).first.click()
        page.locator(INQUIRY_MODAL).wait_for(timeout=INQUIRY_STEP_WAIT_MS)
        _fill_inquiry_form(page, order, message)

        submit = page.locator(INQUIRY_SUBMIT).first
        if not submit.is_enabled():
            raise ParseError("[문의하기] 버튼이 아직 비활성입니다 - 필수 칸이 덜 채워졌습니다.")
        with page.expect_response(
                lambda r: r.request.method == "POST" and urlparse(r.url).path == urlparse(INQUIRY_POST_URL).path,
                timeout=INQUIRY_STEP_WAIT_MS) as posted:
            submit.click()
        response = posted.value
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        code = str(((body or {}).get("data") or {}).get("resultCode"))
        if response.status != 200 or code != "0000":
            note = ((body or {}).get("data") or {}).get("resultMessage") or (body or {}).get("message") or ""
            raise ParseError(f"[문의하기]를 눌렀는데 등록되지 않았습니다 (HTTP {response.status}, resultCode {code} {note}"
                             f"{' / ' + ' / '.join(dialogs) if dialogs else ''}).")
        with contextlib.suppress(PlaywrightTimeoutError):
            page.locator(INQUIRY_MODAL).wait_for(state="detached", timeout=INQUIRY_STEP_WAIT_MS)
        return f"등록 요청 보냄 (화면, resultCode 0000{' · ' + ' / '.join(dialogs) if dialogs else ''})"
    finally:
        page.close()


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """1:1 문의([배송·수거] > [배송문의])를 남기고 확인 문구를 돌려준다.

    취소/품절 주문은 남기지 않고, 상담내역에 이 상품의 같은 문의가 주문일 이후에 이미 있으면
    AlreadyInquired로 넘긴다. 등록은 화면 JS가 보내는 요청을 바로 보내고(_submit_via_api),
    거부되면 화면을 열어 남긴다(_submit_via_form). 어느 쪽이든 등록 뒤 상담내역에 오늘 자로
    올라갔는지 확인한다. 제목(최대 25자)과 내용이 같은 문구다 (사용자 지시).
    """
    order_no = extract_order_no(product_url)
    message = f"{recipient_name.strip()} 배송 언제 시작하나요?"
    order = _order_for_inquiry(context, product_url, order_no)
    existing = _find_listed_inquiry(context, product_url, order, message, order["order_date"])
    if existing is not None:
        raise AlreadyInquired(f"상담내역에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

    done = _submit_via_api(context, product_url, order, message)
    if done is None:
        # 거부 응답이었어도 그 사이 올라갔을 수 있으니 화면을 열기 전에 오늘 자를 한 번 본다.
        posted = _find_listed_inquiry(context, product_url, order, message, date.today(), refresh=True, max_pages=1)
        if posted is not None:
            return f"등록 요청은 거부 응답이었지만 상담내역에 올라감 · 상담내역 확인: {_describe_listed(posted)}"
        done = _submit_via_form(context, product_url, order, message)
    listed = _confirm_inquiry_listed(context, product_url, order, message)
    return f"{done} · 상담내역 확인: {listed}"


# ---------------------------------------------------------------------------
# 주문목록 API 한 번으로 여러 건 답하기 (prepare_batch) - 2026-09-11 실측, 맨 위 docstring
# ---------------------------------------------------------------------------
ORDER_LIST_API_URL = (INQUIRY_API_BASE + "/order/order/order-list?pageNum={page}&pageSize={size}"
                      "&orderDttm1={from_date}&orderDttm2={to_date}&inqrCond=list&reqSpr=mobile"
                      "&stat=&goodsNm=&totalPage=")
# 화면의 [배송조회]가 부르는 요청 - 다섯 파라미터가 전부 있어야 한다(하나라도 빠지면 400).
TRACKING_API_URL = (INQUIRY_API_BASE + "/order/order/order-dlvr-detail-with-gift?orderNum={orderNum}"
                    "&custDstnClssfNum={custDstnClssfNum}&orderRtnClssfCd={orderRtnClssfCd}"
                    "&reltStatCd={reltStatCd}&goodsCd={goodsCd}&unitCd={unitCd}")
TRACKING_API_PARAMS = ("orderNum", "custDstnClssfNum", "orderRtnClssfCd", "reltStatCd", "goodsCd", "unitCd")
LIST_PAGE_SIZE = 50       # 화면은 10건씩이지만 50도 받는다(16건 0.13초) - 보통 한 페이지로 끝난다.
LIST_MAX_PAGES = 4        # 최대 200건. 못 덮은 주문은 화면 폴백으로.
LIST_DAYS_BACK = 30       # 화면 기본 조회 기간(최근 1개월)과 같게.
DEFAULT_COURIER = "택배"  # 배송조회 API에서 택배사명을 못 읽었을 때만

# prepare_batch가 읽어둔 {주문번호: 목록의 그 주문 JSON} - 컨텍스트(=이번 실행의 브라우저)별.
_listed_orders: dict[int, dict[str, dict]] = {}
# 한 실행 안에서 알아낸 {dlvrEntCd: 택배사명}. 같은 코드는 같은 택배사라 두 번째부터는 요청이 없다.
_courier_by_code: dict[str, str] = {}


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문들을 주문목록 API로 미리 통째로 읽어둔다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 세션은 주문상세 화면을
    한 번 열어 받고(_inquiry_session - 로그인이 필요하면 자동 로그인), 실패하면
    아무것도 읽지 않은 것과 같아서 모든 주문이 예전처럼 화면 경로로 간다 - 그래서
    어떤 예외도 밖으로 내보내지 않는다.
    """
    wanted: dict[str, str] = {}  # 주문번호 -> 상품URL (세션을 받을 때 하나 쓴다)
    for order in orders:
        try:
            wanted[extract_order_no(order.product_url)] = order.product_url
        except ParseError:
            continue  # 이런 주문은 어차피 화면 경로에서 같은 이유로 실패한다
    if not wanted:
        return
    sample_no, sample_url = next(iter(wanted.items()))
    to_date = date.today()
    from_date = to_date - timedelta(days=LIST_DAYS_BACK)
    try:
        found: dict[str, dict] = {}
        for page_no in range(1, LIST_MAX_PAGES + 1):
            result = _api_logged_in(context, sample_url, sample_no, ORDER_LIST_API_URL.format(
                page=page_no, size=LIST_PAGE_SIZE, from_date=from_date.isoformat(), to_date=to_date.isoformat()))
            listed = (result or {}).get("orders") or []
            if not listed:
                break
            for order in listed:
                found[str(order.get("orderNum") or "")] = order
            if not (wanted.keys() - found.keys()) or page_no >= int((result or {}).get("totalPage") or 1):
                break
        _listed_orders[id(context)] = found
        common.safe_print(
            f"[nsmall] 주문목록에서 {len(wanted.keys() & found.keys())}/{len(wanted)}건을 미리 읽었습니다.")
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 그냥 화면 경로로 간다
        common.safe_print(f"[nsmall] 주문목록을 읽지 못해 주문마다 상세 화면을 엽니다 ({e}).")


def _item_status(item: dict) -> str:
    """상품 줄의 상태 - '주문 출고지시', '취소 출고지시후취소'처럼 두 값을 붙인다."""
    return f"{item.get('orderRtnClssfCdNm') or ''} {item.get('reltStatCdNm') or ''}".strip()


def _find_item_by_order_option(shipped: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션"으로 상품을 정확히 짚을 수 있으면 그걸 쓴다 - 옵션은
    unitNm("검정, M")에, 상품명은 goodsNm에. 0개나 2개 이상이면 None(개수 비교로)."""
    if len(shipped) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    matched = [it for it in shipped
               if target in normalize_option(it.get("unitNm")) or target in normalize_option(it.get("goodsNm"))]
    return matched[0] if len(matched) == 1 else None


def _select_item(order: dict, order_no: str, order_option: str | None) -> dict:
    """목록의 주문에서 송장을 읽을 상품 줄을 고른다 - 화면 경로와 같은 규칙.

    취소된 줄('취소' 등 CANCELLED_KEYWORDS)은 빼고 본다 - 전부 취소면 OrderCancelled,
    남은 줄에 송장이 하나도 없으면 아직 미발급(지연 표기가 있으면 ShipmentDelayed),
    송장이 여럿인데 상품 수와 다르면 사람이 보게 ParseError.
    """
    items = [it for it in order.get("orderItems") or []
             if str(it.get("orderNum") or order_no) == order_no]
    if not items:
        raise ParseError(f"주문목록 항목에 상품이 없습니다 (주문번호={order_no}).")
    live = [it for it in items if not find_cancelled_keyword(_item_status(it))]
    if not live:
        raise OrderCancelled(
            f"주문 상태가 '{_item_status(items[0])}'입니다 (주문번호={order_no}) - 취소/품절 주문인지 확인해주세요.")
    shipped = [it for it in live if str(it.get("wblNum") or "").strip()]
    if not shipped:
        raise_if_delayed_any([_item_status(it) for it in live], order_no)
        raise TrackingNotAvailableYet(
            f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={_item_status(live[0])}).")
    matched = _find_item_by_order_option(shipped, order_option)
    if matched is not None:
        return matched
    tracking_nos = {re.sub(r"[^0-9]", "", str(it["wblNum"])) for it in shipped}
    if len(tracking_nos) > 1 and len(live) != len(tracking_nos):
        raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
    return shipped[0]


def _courier_name(context: BrowserContext, product_url: str, order_no: str, item: dict) -> tuple[str, bool]:
    """상품 줄의 택배사명과, 그걸 알아내려고 요청을 보냈는지.

    dlvrEntCd가 이미 아는 코드면 요청 없이 답한다. 아니면 화면의 [배송조회]가 부르는
    배송조회 API를 그대로 불러 lscNm을 읽고(_parse_tracking_response) 코드별로 기억해둔다.
    """
    code = str(item.get("dlvrEntCd") or "").strip()
    if code and code in _courier_by_code:
        return _courier_by_code[code], False
    # 목록의 상품 줄에는 orderNum이 없다(주문 단위에만 있다) - 빈 값이면 400.
    params = {k: str(item.get(k) or "") for k in TRACKING_API_PARAMS}
    params["orderNum"] = order_no
    result_data = _api_logged_in(context, product_url, order_no, TRACKING_API_URL.format(**params)) or {}
    _, courier = _parse_tracking_response({"data": {"resultData": result_data}}, order_no)
    if code and courier != DEFAULT_COURIER:
        _courier_by_code[code] = courier
    return courier, True


def _order_date_of(order: dict) -> date | None:
    """주문 단위 orderDttm("20260911081657" - 14자리라 공용 파서(order_date.parse)가 못 읽는다)의 앞 8자리."""
    try:
        return datetime.strptime(str(order.get("orderDttm") or "")[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _delivery_note_of(order: dict) -> str | None:
    """취소 안 된 상품 줄의 도착예정일(dlvrSchdDttm "20260912")을 화면 문구와 같은 파서(eta.from_text)에 넣는다."""
    lines = []
    for item in order.get("orderItems") or []:
        if find_cancelled_keyword(_item_status(item)):
            continue
        raw = str(item.get("dlvrSchdDttm") or "").strip()
        if len(raw) >= 8 and raw[:8].isdigit():
            lines.append(f"도착예정 {raw[:4]}-{raw[4:6]}-{raw[6:8]}")
    return eta_mod.from_text("\n".join(lines)) if lines else None


class _FallBackToScreen(Exception):
    """목록으로 답하다 배송조회 API가 거부돼 화면 경로로 넘긴다는 표시 (밖으로 안 나간다)."""


def _answer_from_list(context: BrowserContext, product_url: str, order: dict, order_no: str,
                      order_option: str | None) -> TrackingResult | None:
    """미리 읽어둔 주문목록 항목으로 결론을 낸다. 택배사명 때문에 요청을 보낸 경우만
    sent_request=True로 표시해서 오케스트레이터가 간격을 지키게 한다.

    배송조회 API가 거부되면(ParseError, 파라미터가 바뀐 경우 등) None - 그 주문만
    예전 화면 경로로 간다. 미발급/취소 판정은 목록만으로 나므로 그대로 올린다.
    """
    sent_request = False

    def fetch() -> TrackingResult:
        nonlocal sent_request
        item = _select_item(order, order_no, order_option)
        try:
            courier, sent_request = _courier_name(context, product_url, order_no, item)
        except ParseError as e:
            common.safe_print(f"[nsmall] 배송조회 API가 거부돼 이 주문은 화면으로 조회합니다 ({e}).")
            raise _FallBackToScreen() from e
        return TrackingResult(tracking_no=re.sub(r"[^0-9]", "", str(item["wblNum"])), courier=courier)

    try:
        result = attach_order_date(_order_date_of(order), fetch,
                                   delivery_note=_delivery_note_of(order))
    except _FallBackToScreen:
        return None
    except AdapterError as e:
        e.sent_request = sent_request
        raise
    result.sent_request = sent_request
    return result

