"""패션플러스(FASHION PLUS) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.fashionplus.co.kr/mypage/order/detail/<주문번호>
- 로그인 폼은 id="login_id" 입력창(Vue 앱)을 쓴다. 로그인이 안 되어 있으면
  보호된 페이지 접근 시 /auth/login 으로 리다이렉트된다.
- FASHIONPLUS_ID/FASHIONPLUS_PW 환경변수가 있으면 세션 만료 시 사람 개입 없이
  완전 자동으로 재로그인한다(SSG/더현대/NS홈쇼핑/11번가/옥션과 동일한 방식).
  로그인 페이지를 실측해 확인한 결과(2026-08-28) reCAPTCHA/Turnstile 같은 봇
  확인 스크립트도, 키보드보안 iframe도 전혀 없었다. FASHIONPLUS_PW를 비워두면
  예전처럼 아이디만 자동 입력하고 사람이 직접 로그인하는 방식으로 동작한다.
  - 비밀번호 입력창은 id/name이 없어(class="textfield"만 있음) 로그인 폼 안의
    input[type=password]로 찾는다 - 페이지 전체에 하나뿐인 것을 확인했다.
  - 로그인 실패는 alert이 아니라 POST /auth/login 응답으로 알려준다: 401 +
    {"message": "아이디 또는 비밀번호를 잘못 입력하셨습니다."} 형태라, 이
    응답을 붙잡아 실패 사유째로 올린다(화면에도 같은 문구가 토스트로 뜨지만
    응답 쪽이 훨씬 확실하다).
  - 로그인 전에 "로그인 상태 유지" 체크박스를 켜둔다(기본값 꺼짐). 자동/수동
    로그인 양쪽 다 켜서 재로그인 주기를 늘린다(네이버 어댑터와 같은 이유).
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

import contextlib
import json
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    AlreadyInquired,
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled_any,
    raise_if_delayed_any,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#login_id"
# 비밀번호 입력창에는 id/name이 없다 - 로그인 폼(#login_form) 안의 password 타입으로 찾는다.
LOGIN_PW_SELECTOR = "#login_form input[type='password']"
LOGIN_BUTTON_SELECTOR = "#login_form button.mm_btn.__btn_lg_primary__"
# input 자체는 화면에서 숨겨져 있고(커스텀 스타일) label만 보이는 형태다.
KEEP_LOGIN_SELECTOR = "#login_form label.mm_form-check:has-text('로그인 상태 유지') input[type='checkbox']"
LOGIN_API_PATH = "/auth/login"

DOMAINS = {"fashionplus.co.kr", "www.fashionplus.co.kr"}
SITE_KEY = "fashionplus"
# 요청 간격. 조회가 주문상세 API 호출 하나(0.1~0.3초)라 기본 간격(1.5~4초)이
# 시간의 전부였다 - 4910과 같은 근거로 좁힌다 (2026-09-02).
REQUEST_GAP = (0.5, 1.2)

ORDER_DETAIL_URL = "https://www.fashionplus.co.kr/mypage/order/detail/{order_no}"

GOODSFLOW_API_URL = "https://trace.goodsflow.com/VIEW/api/tracking"
GOODSFLOW_MEMBER_CODE = "fashionplus"

DEFAULT_COURIER = "택배"  # goodsflow 응답에 택배사명이 비어있을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게
LOGIN_RESPONSE_TIMEOUT_MS = 15 * 1000  # 로그인 API 응답 대기

TRACKING_LINK_TEXT = "배송조회"
NOT_YET_PATTERNS = ["배송준비중", "결제완료", "입금대기", "주문확인중"]
# 상품별 주문상태 칸. 주문상세는 li.mm_product-item 하나가 상품 하나이고 그 안의
# p.text_status 에 '결제완료' / '배송중' / '환불완료' 같은 상태가 들어있다.
# 판정은 반드시 이 칸만 본다 - 화면 전체 텍스트에는 왼쪽 메뉴의 "배송지연 신고",
# "품절취소 신고", "취소/반품/교환/환불 내역" 같은 항목이 항상 같이 잡혀서, 취소·
# 지연 판정이 주문 상태가 아니라 메뉴 글자에 걸린다 (2026-09-11 실측: '환불완료'
# 주문 2건이 메뉴의 "배송지연 신고"에 걸려 '발송지연' 스킵으로 잘못 기록됐다).
STATUS_SELECTOR = "li.mm_product-item p.text_status"


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
    return common.looks_like_login_page(page, lambda url: "/auth/login" in url, needs_password=False)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("FASHIONPLUS_ID"))


def _enable_keep_login(page) -> None:
    """로그인하기 전에 "로그인 상태 유지"를 켜둔다 (기본값은 꺼짐).

    사이트가 사용자에게 정상적으로 제공하는 옵션이고, 켜두면 로그인 쿠키가
    오래 유지돼 재로그인 주기가 길어진다(네이버 어댑터와 같은 이유).
    체크박스가 없거나 이미 켜져 있으면 아무것도 하지 않는다.
    """
    locator = page.locator(KEEP_LOGIN_SELECTOR)
    if locator.count() == 0:
        return
    try:
        if not locator.first.is_checked():
            # input이 화면에서 숨겨져 있어(label만 보임) force=True가 필요하다.
            locator.first.check(force=True)
    except Exception:
        pass


def _auto_login(page) -> bool:
    """FASHIONPLUS_ID/FASHIONPLUS_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 경우를 위해 수동
    로그인 경로를 그대로 남겨뒀다).

    로그인 실패는 POST /auth/login의 401 응답으로 판별해 사유째로 올린다 -
    화면만 보고 있으면 "로그인 페이지에서 안 벗어남"으로만 보여서, 비밀번호가
    틀린 건지 추가 인증이 필요한 건지 알 수 없다.
    """
    login_id = os.environ.get("FASHIONPLUS_ID")
    login_pw = os.environ.get("FASHIONPLUS_PW")
    if not login_id or not login_pw:
        return False

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    _enable_keep_login(page)

    def _is_login_api(response) -> bool:
        return response.request.method == "POST" and LOGIN_API_PATH in response.url

    try:
        with page.expect_response(_is_login_api, timeout=LOGIN_RESPONSE_TIMEOUT_MS) as resp_info:
            page.locator(LOGIN_BUTTON_SELECTOR).first.click()
        response = resp_info.value
    except PlaywrightTimeoutError:
        response = None  # 응답을 못 잡아도 아래 화면 상태 확인으로 판정한다

    if response is not None and response.status != 200:
        raise BlockedError(f"패션플러스 자동 로그인이 거부됐습니다: {_login_error_message(response)}")

    elapsed_ms = 0
    while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500

    raise BlockedError(
        "패션플러스 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
        "(추가 본인인증을 요구받았을 수 있습니다 - --headless 없이 실행해 브라우저 창을 확인해주세요)."
    )


def _login_error_message(response) -> str:
    """로그인 실패 응답({"message": "..."})에서 사유 문구를 꺼낸다."""
    try:
        message = (response.json() or {}).get("message")
    except Exception:
        message = None
    return (message or "").strip() or f"HTTP {response.status}"


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _item_statuses(page) -> list[str]:
    """상품별 주문상태 값 (화면 순서). 없으면 빈 목록.

    '배송중' 상태 칸에는 [배송조회] 링크가 같은 칸에 붙어 innerText가
    "배송중
배송조회"로 나오므로 첫 줄만 상태로 본다.
    """
    statuses = []
    for text in page.locator(STATUS_SELECTOR).all_inner_texts():
        first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
        if first_line:
            statuses.append(first_line)
    return statuses


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
        # 상태 칸만 본다 (STATUS_SELECTOR 주석 참고). 상품이 여러 줄이면 하나라도
        # 진행 중(결제완료 등)이면 아직 미발급, 아니면 취소/품절/환불완료,
        # 그다음 지연 순으로 본다 - 준비 중인 줄이 있는데 취소 줄 하나로
        # 취소로 분류하면 다음 실행에서 다시 조회되지 않는다.
        statuses = _item_statuses(page)
        status_text = " / ".join(statuses)
        if any(p in status_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={status_text}).")
        raise_if_cancelled_any(statuses, order_no)
        raise_if_delayed_any(statuses, order_no)
        raise ParseError(
            f"배송조회 링크를 찾지 못했습니다 (주문번호={order_no}, 상태={status_text or '상태 칸 없음'}).")

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
    courier = common.normalize_courier((chosen.get("logisticsName") or "").strip() or DEFAULT_COURIER)
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def _goto_logged_in(page, url: str, headless: bool) -> None:
    """주소를 열고, 로그인 화면이면 자동(또는 창이 있으면 수동) 로그인한 뒤 다시 연다.

    송장조회(get_tracking)와 문의 등록(post_inquiry)이 같은 경로를 쓴다.
    """
    page.goto(url, wait_until="domcontentloaded")
    if not _looks_like_login_page(page):
        return
    if _auto_login(page):
        common.safe_print("[fashionplus] 로그인 세션이 없어 자동 로그인했습니다.")
    elif headless:
        raise BlockedError(
            "패션플러스 로그인이 필요합니다. .env에 FASHIONPLUS_PW를 넣으면 자동 로그인하고, "
            "비밀번호를 저장하지 않으려면 --headless 없이 실행해 직접 로그인해주세요."
        )
    else:
        _prefill_login_id(page)
        _enable_keep_login(page)
        common.safe_print("[fashionplus] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
        common.safe_print("[fashionplus] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
        if not _wait_for_manual_login(page):
            raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
    page.goto(url, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    url = ORDER_DETAIL_URL.format(order_no=order_no)
    page = context.new_page()
    try:
        _goto_logged_in(page, url, headless)

        # 화면이 아직 덜 그려진 채로 읽으면 '아직 미발급'으로 잘못 넘길 수 있다
        # (조용히 틀리는 쪽이라 특히 위험하다). 상품별 상태 칸이 뜰 때까지만
        # 기다린다 - 보통은 이미 있어서 그냥 지나간다. 예전에는 [배송조회]나
        # 진행중 문구를 기다렸는데, 환불완료 주문은 둘 다 없어 매번 대기 시간을
        # 다 채웠다. 안 뜨면 아래에서 '상태 칸 없음' ParseError로 남는다.
        try:
            page.wait_for_selector(STATUS_SELECTOR, timeout=common.ORDER_RENDER_WAIT_MS)
        except PlaywrightTimeoutError:
            pass
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()


# --------------------------------------------------------------------------
# 1:1 문의 - "○○○ 배송 언제 시작하나요?" (사용자 요청 2026-09-16)
# --------------------------------------------------------------------------
# 사람이 하던 순서: 주문상세 → 왼쪽 메뉴 [1:1 문의하기] → 1차 문의유형 '배송문의' → 2차
# '단순 배송일 문의' → 주문번호에서 그 주문 선택 → 문의 상품(하나 뜸) 선택 → 문의 제목·
# 내용에 "○○○ 배송 언제 시작하나요?" → [SMS 수집을 동의하며, SMS 답변수신] 체크 →
# 휴대폰 번호 → [문의하기] → 왼쪽 메뉴 [1:1 문의 내역]에서 등록 확인.
#
# 실측(2026-09-16):
# - 문의 폼(/mypage/mall-qna/write)은 Vue 화면이고 GET /mypage/mall-qna/fetch-order-info
#   (최근 3개월 주문 전부, 330KB)로 주문번호·상품 선택지를 채운다. <select>는 id/name이
#   없어 바로 앞의 제목(h6.mm_text-label)으로 찾는다. 1차 유형은 value=4(배송문의), 그 뒤
#   2차 유형이 채워지고 value=11(단순 배송일 문의). 주문번호 select의 value가 주문번호,
#   그 뒤 문의 상품 select의 value는 옵션 id(orderedGoodsList[].optionId). 2차·상품 선택은
#   요청을 보내지 않는다(전부 화면 안에서 채움).
# - 목록에 없는 주문(3개월보다 오래됨)은 select에 없어 고를 수 없다 → ParseError.
# - [1:1 문의 내역](/mypage/mall-qna)은 GET /mypage/mall-qna/fetch?page=N(20건, 최신순,
#   x-requested-with: XMLHttpRequest 헤더가 없으면 400)이 JSON으로 준다 - items[]에
#   id(문의번호)·createdAt·orderId·title·content·isAnswered·answerContent·answeredAt이
#   다 있어 화면을 열 일이 없다. 비로그인은 401 {"message":"로그인이 필요합니다."}.
#   질문에 주문번호가 orderId로 따로 오므로 주문번호+제목으로 우리 문의를 고른다.
# - 등록은 [문의하기]를 눌러 화면이 보내는 요청 그대로 - POST /mypage/mall-qna (multipart:
#   typeCode1=4, typeCode2=11, orderId, ...). 응답 상태만 보고(4xx면 message), 화면에 뜨는
#   모달 "1:1 문의 작성이 완료되었습니다."를 읽은 뒤 목록 첫 쪽에 오늘 자로 올라갔는지
#   확인한다(주소는 /write에 그대로 머문다). [SMS 답변수신] 체크는 숨은 input이 아니라
#   label을 눌러야 켜지고, [문의하기] 버튼(.mm_foot)은 .m_modal-inquiry-inner 밖 form 안에 있다.
# - 문의 화면에는 page.route(_guard_inquiry_page)로 광고·분석 호스트와 이미지를 끊는다 - 한 건
#   1.4~1.6초 → 0.9초(주문상세 0.57→0.23초, 폼 0.44→0.14초). 주의: page.route가 걸린 페이지에는
#   context.route가 불리지 않으므로, 시험에서 등록 POST를 막으려면 context.route가 아니라 이
#   함수를 바꿔 끼워야 한다(2026-09-16 그걸 놓쳐 시험 두 번이 실등록돼 같은 문의가 세 건 남았고,
#   사이트에 삭제 기능이 없어 되돌리지 못했다).
QNA_WRITE_URL = "https://www.fashionplus.co.kr/mypage/mall-qna/write"
QNA_LIST_URL = "https://www.fashionplus.co.kr/mypage/mall-qna"
QNA_LIST_API = "https://www.fashionplus.co.kr/mypage/mall-qna/fetch?page={page_no}"
QNA_API_HEADERS = {"accept": "application/json, text/plain, */*", "x-requested-with": "XMLHttpRequest"}
QNA_SUBMIT_URL_MARK = "/mypage/mall-qna"  # 등록 요청 주소 (POST, multipart - GET fetch-*는 뺀다)
QNA_DONE_TEXT = "완료되었습니다"          # 등록 뒤 모달 "1:1 문의 작성이 완료되었습니다."
QNA_TYPE_CODE = "4"                       # 1차 문의유형: 배송문의
QNA_SUBTYPE_CODE = "11"                   # 2차 문의유형: 단순 배송일 문의
QNA_SMS_PHONE = "01022178032"             # SMS 답변수신 번호 (사용자 지정)
QNA_FORM_SELECTOR = "form:has(.m_modal-inquiry-inner)"   # [문의하기] 버튼(.mm_foot)은 inner 밖, form 안에 있다
QNA_SELECT_TEMPLATE = "h6.mm_text-label:has-text('{label}') + div.mm_form-select select"
QNA_TITLE_SELECTOR = "h6.mm_text-label:has-text('문의 제목') + div.mm_form-text input[type='text']"
QNA_CONTENT_SELECTOR = "h6.mm_text-label:has-text('문의 내용') + div.mm_form-textarea textarea"
QNA_SMS_LABEL_SELECTOR = "label.mm_form-check:has-text('SMS')"
QNA_SMS_CHECK_SELECTOR = "label.mm_form-check:has-text('SMS') input[type='checkbox']"
QNA_SMS_PHONE_SELECTOR = "li:has(label.mm_form-check:has-text('SMS')) input[type='text']"
QNA_SUBMIT_SELECTOR = ".mm_foot button:has-text('문의하기')"
QNA_PAGE_SIZE = 20
QNA_MAX_PAGES = 5            # 최대 100건 - 주문일 이전 문의가 나오면 그 전에 멈춘다.
QNA_STEP_WAIT_MS = 10 * 1000
QNA_HISTORY_TRIES = 3        # 등록 직후 목록에 아직 없으면 잠깐 뒤 다시 본다.
QNA_HISTORY_RETRY_GAP_SEC = 1.5
QNA_STATE_ANSWERED = "답변완료"
QNA_STATE_WAITING = "답변대기"
# 문의 화면에서 통과시키는 호스트. 주문상세·문의 폼은 광고·분석 스크립트(googletagmanager,
# blux.ai, criteo, facebook, daangn, megadata …)를 20곳 넘게 부르는데 문의에는 하나도 필요 없다.
INQUIRY_ALLOWED_HOSTS = ("fashionplus.co.kr",)
# 송장조회용 컨텍스트가 막는 무거운 리소스(browser.BLOCKED_RESOURCE_TYPES와 같은 값). page.route를
# 걸면 context.route는 불리지 않아(롯데아이몰 실측) 여기서 같이 막아야 이미지 150건이 다시 안 간다.
INQUIRY_HEAVY_RESOURCES = {"image", "media", "font"}


def _guard_inquiry_page(route) -> None:
    """문의 화면 전용 라우팅 - 패션플러스 밖 호스트와 이미지·폰트는 끊어 화면을 가볍게 한다."""
    request = route.request
    host = urlparse(request.url).netloc.lower()
    allowed = any(host == h or host.endswith("." + h) for h in INQUIRY_ALLOWED_HOSTS)
    if allowed and request.resource_type not in INQUIRY_HEAVY_RESOURCES:
        route.continue_()
    else:
        route.abort()


def _inquiry_text(recipient_name: str) -> str:
    return f"{recipient_name.strip()} 배송 언제 시작하나요?"


def _fetch_qna_page(context: BrowserContext, page_no: int) -> dict | None:
    """[1:1 문의 내역] 한 쪽(JSON). 로그인이 풀렸거나 못 읽으면 None."""
    try:
        response = context.request.get(QNA_LIST_API.format(page_no=page_no), headers=QNA_API_HEADERS)
    except Exception:  # noqa: BLE001 - 통신 실패는 '못 읽음'
        return None
    if response.status != 200:
        return None
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def _parse_when(value) -> datetime | None:
    """"2026-09-16T11:28:12" → datetime. 없거나 못 읽으면 None."""
    if not value:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
    return None


def _qna_rows(data: dict) -> list[dict]:
    """목록 JSON의 줄마다 {inquiry_id, state, written_at, order_no, title, content, answer, answered_at}."""
    rows: list[dict] = []
    for item in data.get("items") or []:
        written_at = _parse_when(item.get("createdAt"))
        if written_at is None:
            continue
        rows.append({
            "inquiry_id": str(item.get("id") or ""),
            "state": QNA_STATE_ANSWERED if item.get("isAnswered") else QNA_STATE_WAITING,
            "written_at": written_at,
            "order_no": str(item.get("orderId") or ""),
            "title": str(item.get("title") or ""),
            "content": str(item.get("content") or ""),
            "answer": (str(item.get("answerContent") or "").strip() or None) if item.get("isAnswered") else None,
            "answered_at": _parse_when(item.get("answeredAt")),
        })
    return rows


def _same_message(row: dict, message: str) -> bool:
    wanted = re.sub(r"\s+", "", message)
    return any(wanted in re.sub(r"\s+", "", row[key]) for key in ("title", "content"))


def _find_listed_qna(context: BrowserContext, order_no: str, message: str, since: date, *,
                     max_pages: int = QNA_MAX_PAGES, inquiry_id: str | None = None,
                     first_page: dict | None = None) -> dict | None:
    """[1:1 문의 내역]에서 since 이후에 쓴, 이 주문번호의 우리 문구 문의를 찾는다 (최신순).

    문의번호(inquiry_id)가 있으면 그 줄이면 바로 맞는다. 목록을 못 읽으면 ParseError -
    모르는 채로 등록하지 않는다.
    """
    for page_no in range(1, max_pages + 1):
        data = first_page if (page_no == 1 and first_page is not None) else _fetch_qna_page(context, page_no)
        if data is None:
            raise ParseError("[1:1 문의 내역]을 읽지 못했습니다 (로그인 세션이 없거나 화면이 바뀜).")
        rows = _qna_rows(data)
        if not rows:
            return None
        for row in rows:
            if row["written_at"].date() < since:
                return None
            if (inquiry_id and row["inquiry_id"] == inquiry_id) or (
                    row["order_no"] == order_no and _same_message(row, message)):
                return row
        total = int(data.get("totalCount") or 0)
        if page_no * QNA_PAGE_SIZE >= total:
            return None
    return None


def _describe_listed(row: dict) -> str:
    return f"{row['state']} {row['written_at']:%Y-%m-%d %H:%M} (문의번호 {row['inquiry_id']})"


def _confirm_qna_listed(context: BrowserContext, order_no: str, message: str) -> str:
    """[1:1 문의 내역] 첫 쪽에 이 주문의 우리 문의가 오늘 자로 올라갔는지 확인한다."""
    for attempt in range(1, QNA_HISTORY_TRIES + 1):
        found = _find_listed_qna(context, order_no, message, date.today(), max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < QNA_HISTORY_TRIES:
            common.sleep(QNA_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"[문의하기]를 눌렀지만 [1:1 문의 내역]에서 확인되지 않았습니다. 다시 남기기 전에 패션플러스 "
        f"마이쇼핑 → 1:1 문의 내역에 '{message}'가 있는지 직접 확인해주세요.")


def _form_select(page, label: str):
    return page.locator(QNA_FORM_SELECTOR).locator(QNA_SELECT_TEMPLATE.format(label=label)).first


def _select_when_ready(page, label: str, value: str, what: str) -> None:
    """제목(label) 아래 select에 value 선택지가 생기길 기다렸다가 고른다 (Vue가 뒤늦게 채운다)."""
    select = _form_select(page, label)
    try:
        select.wait_for(state="attached", timeout=QNA_STEP_WAIT_MS)
        select.wait_for_function(
            "(el, v) => [...el.options].some(o => o.value === v)", arg=value, timeout=QNA_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        options = select.evaluate("el => [...el.options].map(o => o.text + '=' + o.value)") if select.count() else []
        raise ParseError(f"문의 폼의 '{label}'에 {what}이(가) 없습니다 (선택지={options[:8]!r}).") from None
    select.select_option(value)
    if select.input_value() != value:
        raise ParseError(f"문의 폼의 '{label}'이(가) {what}(으)로 선택되지 않았습니다.")


def _select_first_product(page) -> str:
    """'문의 상품'에서 (빈 값이 아닌) 첫 상품을 고르고 그 이름을 돌려준다. 여러 개면 첫 것."""
    select = _form_select(page, "문의 상품")
    try:
        select.wait_for_function(
            "el => [...el.options].some(o => o.value !== '')", timeout=QNA_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError("문의 폼의 '문의 상품'에 고를 상품이 뜨지 않았습니다.") from None
    options = select.evaluate("el => [...el.options].filter(o => o.value !== '').map(o => [o.value, o.text.trim()])")
    value, name = options[0]
    select.select_option(value)
    if select.input_value() != value:
        raise ParseError("문의 폼의 '문의 상품'이 선택되지 않았습니다.")
    return name if len(options) == 1 else f"{name} (상품 {len(options)}개 중 첫 번째)"


def _fill_checked(page, selector: str, value: str, what: str) -> None:
    box = page.locator(QNA_FORM_SELECTOR).locator(selector).first
    box.fill(value)
    if box.input_value() != value:
        raise ParseError(f"{what}이(가) 입력되지 않았습니다.")


def _order_date_from_page(page) -> date:
    """주문상세의 '(신청일: 2026-09-16)'. 못 읽으면 넉넉히 14일 전부터 본다."""
    found = order_date_mod.from_text(page.inner_text("body"))
    return found or (date.today() - timedelta(days=14))


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """[1:1 문의하기]에 배송문의 > 단순 배송일 문의로 "○○○ 배송 언제 시작하나요?"를 남기고, [1:1 문의 내역]에서 확인한 문구를 돌려준다.

    주문상세에서 취소/품절이면 남기지 않고, 배송지의 받는사람이 수령인과 다르면 엉뚱한
    주문이라 멈춘다. [1:1 문의 내역]에 이 주문의 같은 문의가 주문일 이후에 이미 있으면
    AlreadyInquired. 제목과 내용에 같은 문구를 적고 SMS 답변수신(휴대폰 QNA_SMS_PHONE)을 켠다.
    """
    order_no = extract_order_no(product_url)
    message = _inquiry_text(recipient_name)
    page = context.new_page()
    page.route("**/*", _guard_inquiry_page)   # 광고·분석 스크립트를 끊으면 화면이 더 빨리 뜬다
    try:
        _goto_logged_in(page, ORDER_DETAIL_URL.format(order_no=order_no), headless)
        try:
            page.wait_for_selector(STATUS_SELECTOR, timeout=common.RENDER_WAIT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise ParseError(f"주문상세가 그려지지 않았습니다 (주문번호={order_no}).") from None
        statuses = _item_statuses(page)
        raise_if_cancelled_any(statuses, order_no)
        # 배송지 정보(받는사람)는 상태 칸보다 늦게 그려지기도 해서 잠깐 기다려 본다
        # (실측: 상태 칸이 뜬 직후 본문에 아직 받는사람이 없어 헛짚었다).
        if recipient_name.strip() and not common.wait_for_text(
                page, recipient_name.strip(), common.ORDER_RENDER_WAIT_MS):
            raise ParseError(f"주문상세의 받는사람이 수령인 '{recipient_name}'과 다릅니다 (주문번호={order_no}).")
        order_date = _order_date_from_page(page)

        existing = _find_listed_qna(context, order_no, message, order_date)
        if existing is not None:
            raise AlreadyInquired(f"[1:1 문의 내역]에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

        page.goto(QNA_WRITE_URL, wait_until="domcontentloaded")
        if _looks_like_login_page(page):
            raise BlockedError("1:1 문의 폼을 여는데 로그인 화면으로 넘어갔습니다.")
        _select_when_ready(page, "1차 문의유형", QNA_TYPE_CODE, "'배송문의'")
        _select_when_ready(page, "2차 문의유형", QNA_SUBTYPE_CODE, "'단순 배송일 문의'")
        _select_when_ready(page, "주문번호", order_no, f"주문번호 {order_no}")
        product = _select_first_product(page)
        _fill_checked(page, QNA_TITLE_SELECTOR, message, "문의 제목")
        _fill_checked(page, QNA_CONTENT_SELECTOR, message, "문의 내용")
        sms = page.locator(QNA_FORM_SELECTOR).locator(QNA_SMS_CHECK_SELECTOR).first
        if not sms.is_checked():
            # input은 숨겨져 있고 label이 상태를 바꾼다 - 숨은 input을 force로 눌러도
            # 안 켜진다(실측 "Clicking the checkbox did not change its state").
            page.locator(QNA_FORM_SELECTOR).locator(QNA_SMS_LABEL_SELECTOR).first.click()
        if not sms.is_checked():
            raise ParseError("'SMS 답변수신' 체크가 켜지지 않았습니다.")
        _fill_checked(page, QNA_SMS_PHONE_SELECTOR, QNA_SMS_PHONE, "휴대폰 번호")

        dialogs: list[str] = []

        def _on_dialog(dialog) -> None:
            dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", _on_dialog)

        def _is_submit(response) -> bool:
            return (response.request.method != "GET" and "fashionplus.co.kr" in response.url
                    and QNA_SUBMIT_URL_MARK in response.url)

        submit_status: int | None = None
        submit_note = ""
        try:
            with page.expect_response(_is_submit, timeout=QNA_STEP_WAIT_MS) as resp_info:
                page.locator(QNA_FORM_SELECTOR).locator(QNA_SUBMIT_SELECTOR).first.click()
            response = resp_info.value
            submit_status = response.status
            if submit_status >= 400:
                with contextlib.suppress(Exception):
                    submit_note = str((response.json() or {}).get("message") or "")
        except PlaywrightTimeoutError:
            pass   # 응답을 못 잡아도 목록 확인이 기준이다
        if submit_status is not None and submit_status >= 400:
            raise ParseError(f"[문의하기]가 거부되었습니다 (HTTP {submit_status}): {submit_note or dialogs or '사유 없음'}")
        # 완료 모달("1:1 문의 작성이 완료되었습니다.")이 뜨면 그 문구를 싣는다 - 없어도 목록 확인이 기준.
        done = "등록 완료"
        if common.wait_for_text(page, QNA_DONE_TEXT, common.MODAL_RENDER_WAIT_MS):
            with contextlib.suppress(PlaywrightError):
                done = next((ln.strip() for ln in page.inner_text("body").splitlines()
                             if QNA_DONE_TEXT in ln), done)
        listed = _confirm_qna_listed(context, order_no, message)
        return f"{done} (배송문의 > 단순 배송일 문의, {product}) · 1:1 문의 내역 확인: {listed}"
    finally:
        page.close()


def fetch_inquiry_answer(context: BrowserContext, product_url: str, recipient_name: str, *,
                         since: date, inquiry_id: str | None = None, headless: bool = False) -> dict | None:
    """이 주문에 남긴 1:1 문의의 상태·답변 - {inquiry_id, state, written_on, answer, answered_on}, 없으면 None.

    [1:1 문의 내역] JSON(request)으로 세션을 확인하고(없으면 화면을 열어 자동 로그인) 주문일부터의
    목록에서 이 주문번호의 우리 문구 줄을 찾는다 - 목록에 답변까지 같이 오므로 상세를 열 일이 없다.
    """
    order_no = extract_order_no(product_url)
    message = _inquiry_text(recipient_name)
    data = _fetch_qna_page(context, 1)
    if data is None:
        page = context.new_page()
        try:
            _goto_logged_in(page, QNA_LIST_URL, headless)
        finally:
            page.close()
        data = _fetch_qna_page(context, 1)
        if data is None:
            raise BlockedError("패션플러스 [1:1 문의 내역]을 읽지 못했습니다 (로그인 세션이 없습니다).")
    found = _find_listed_qna(context, order_no, message, since, inquiry_id=inquiry_id, first_page=data)
    if found is None:
        return None
    return {
        "inquiry_id": found["inquiry_id"],
        "state": found["state"],
        "written_on": f"{found['written_at']:%Y-%m-%d}",
        "answer": found["answer"],
        "answered_on": f"{found['answered_at']:%Y-%m-%d}" if found["answered_at"] else None,
    }
