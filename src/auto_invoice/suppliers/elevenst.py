"""11번가(buy.11st.co.kr) 공급사 어댑터.

리버스엔지니어링 결과:
- 샵마인 엑셀의 "상품URL" 컬럼에 실제로 들어있는 형태(내보내기 파일에서 확인):
  https://buy.11st.co.kr/order/BuyManager.tmall?method=getOrderDetailInfo&ordNo=<주문번호>&isSSL=Y
  화면상으로는 주문목록에서 "상세보기"가 POST 폼 전송(BuyManager.tmall)으로
  열리지만, 위 URL은 GET으로도 그대로 열리는 것을 확인했다. 그래서 다른
  어댑터와 똑같이 product_url을 goto하기만 하면 된다.
- 로그인이 안 되어 있으면 login.11st.co.kr/auth/v2/login?...&returnURL=... 으로
  리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#memId, 비밀번호 input#memPwd,
  로그인 버튼 button#loginButton. 사용자가 "첫 로그인부터 쿠키로 자동
  로그인"을 요청했고, 실제로 아이디+비밀번호를 채우고 로그인 버튼을 자동
  클릭해도 캡차 등에 막히지 않는 것을 확인했다 (SSG/더현대/NS홈쇼핑과 동일한
  패턴). 그래서 ELEVENST_ID/ELEVENST_PW 환경변수로 완전 자동 로그인하고,
  로그인 세션은 storage_state(auth/elevenst_state.json)에 저장되어 이후
  실행부터는 쿠키만으로 바로 조회된다.
  ("로그인 상태 유지" 체크박스(#lbAutoLogin)도 있지만, 체크하면 공용PC 주의
  안내 모달(#arModalLoginNudge)이 떠서 로그인 버튼 클릭 자체를 가로막는 것을
  확인했다. storage_state로 쿠키가 이미 보존되므로 체크하지 않는다.)
- 주문상세 페이지에는 송장번호가 없다. 상품(주문상태 칸)마다
  <a href="javascript:goDeliveryTracking('<dlvNo>');">배송조회</a> 링크만 있고,
  이 dlvNo는 11번가 내부 배송번호이지 송장번호가 아니다 (예: dlvNo 2721476603 ->
  실제 송장번호 304318936344). 실제 송장번호/택배사는 그 링크가 여는
  https://buy.11st.co.kr/delivery/trace.tmall?dlvNo=<dlvNo> 페이지의
  div.delivery_info 안 "택배사"/"송장번호" 필드에 있다. 이 페이지에는 주문번호도
  같이 나와서, 엉뚱한 주문의 송장을 가져오지 않았는지 검증할 수 있다.
- 미발송 판단: 주문상세 페이지 하단에는 "배송진행순서 ... 3.배송준비중 ..."
  같은 고정 안내문이 항상 붙어 있어서, 본문 전체에서 "배송준비중"을 찾으면
  발송된 주문까지 미발송으로 오판한다. 그래서 안내문 시작("알아두세요!") 앞
  구간만 잘라서 상태 문구를 본다.
- 상품이 여러 개면 "배송조회" 링크도 여러 개 뜬다. 11번가 주문상세는 상품
  한 개가 <tr> 하나라서, 링크가 속한 <tr>의 텍스트에 샵마인 엑셀의 "주문옵션"
  값이 들어있는지로 어느 상품인지 정확히 특정할 수 있다. 특정할 수 없으면
  (다른 어댑터와 동일한 안전 규칙) 전부 조회해서 실제로 서로 다른 송장인지
  비교하고, 다르면 사람이 확인하도록 예외를 던진다.
"""

from __future__ import annotations

import contextlib
import os
import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..models import TrackingResult
from . import common
from .base import (
    AlreadyInquired,
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#memId"
LOGIN_PW_SELECTOR = "#memPwd"
LOGIN_BUTTON_SELECTOR = "#loginButton"

# 로그인 버튼을 누른 직후 뜨는 모달들. 이걸 처리하지 않으면 로그인은 됐는데도
# 화면이 로그인 페이지에 머물러 있어 "로그인 실패"로 오판한다.
#   - "PC 로그인 상태 유지 안내": "네, 좋아요"를 눌러야 쿠키가 오래 유지되므로
#     (사용자가 원한 "쿠키로 자동 로그인"에 유리) 등록 버튼을 누른다.
#   - "간편 로그인(패스키) 등록 안내": 굳이 등록할 필요가 없어 닫기만 한다.
POST_LOGIN_MODAL_BUTTONS = [
    "button[modal-auto-action='register']",  # 로그인 상태 유지 - 네, 좋아요
    "#btnSpLoginClose",  # 간편 로그인 등록 안내 - 닫기
]

DOMAINS = {"buy.11st.co.kr", "11st.co.kr", "www.11st.co.kr", "m.11st.co.kr"}
SITE_KEY = "elevenst"

# 주문당 상세 화면 1개를 여는 사이트. 기본 간격(1.5~4초)은 봇 확인이 잘 뜨는
# 사이트를 기준으로 잡은 값이라, 화면 하나 여는 데 1.2초쯤 걸리는 여기서는
# 조회 시간의 절반이 그냥 쉬는 시간이었다 (2026-09-04 실측: 롯데아이몰 6건
# 15.5초 중 순수 조회 7.5초). 네이버와 같은 간격으로 둔다 - 사람이 주문을
# 하나씩 눌러 보는 속도다.
REQUEST_GAP = (1.0, 2.0)


LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초

TRACE_URL = "https://buy.11st.co.kr/delivery/trace.tmall?dlvNo={dlv_no}"
TRACKING_LINK_SELECTOR = "a[href*='goDeliveryTracking']"
DLV_NO_PATTERN = re.compile(r"goDeliveryTracking\('([^']+)'")

# 주문상세 본문에서 이 문구부터는 고정 안내문이라 상태 판단에서 제외한다.
GUIDE_ANCHOR = "알아두세요!"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "입금대기중", "주문확인중"]

# 배송추적 페이지의 "택배사"/"송장번호" 필드
DELIVERY_FIELD_SELECTOR = "div.delivery_info div.field"
COURIER_FIELD_LABEL = "택배사"
TRACKING_FIELD_LABEL = "송장번호"
# 배송추적 페이지 하단 "주문정보"의 항목명/값 (주문번호 검증용)
INFO_ITEM_SELECTOR = "p.prd_info"
ORDER_NO_LABEL = "주문번호"

# "CJ대한통운 1588-1255"처럼 택배사명 뒤에 고객센터 전화번호가 붙어서 나온다.
COURIER_PHONE_PATTERN = re.compile(r"\s*[0-9][0-9\-]{5,}\s*$")

DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(page, lambda url: "login.11st.co.kr" in url)


def _dismiss_post_login_modals(page: Page) -> None:
    """로그인 직후 뜨는 안내 모달을 닫는다 (뜨지 않았으면 아무것도 하지 않는다)."""
    for selector in POST_LOGIN_MODAL_BUTTONS:
        try:
            button = page.locator(selector)
            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=3000)
                page.wait_for_timeout(500)
        except Exception:
            # 모달이 이미 닫혔거나 페이지가 넘어가는 중일 수 있다 - 무시하고 계속 대기한다.
            continue


def _auto_login(page: Page) -> bool:
    """ELEVENST_ID/ELEVENST_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    SSG/더현대/NS홈쇼핑 어댑터와 동일한 패턴 - 11번가도 자동 클릭 로그인이
    캡차 등에 막히지 않는 것을 확인했다.
    """
    login_id = os.environ.get("ELEVENST_ID")
    login_pw = os.environ.get("ELEVENST_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "11번가 로그인이 필요하지만 ELEVENST_ID/ELEVENST_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    page.click(LOGIN_BUTTON_SELECTOR)

    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        _dismiss_post_login_modals(page)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500
    return False


def _order_area_text(page: Page) -> str:
    """페이지 하단 고정 안내문("배송진행순서" 등)을 잘라낸 본문.

    안내문에 "배송준비중" 같은 단어가 항상 들어있어서, 자르지 않으면 이미
    발송된 주문도 미발송으로 오판한다.
    """
    text = page.inner_text("body")
    idx = text.find(GUIDE_ANCHOR)
    return text[:idx] if idx != -1 else text


def _read_field_value(page: Page, label: str) -> str | None:
    """배송추적 페이지 div.delivery_info 안에서 dt가 label인 field의 dd 텍스트."""
    fields = page.locator(DELIVERY_FIELD_SELECTOR)
    for i in range(fields.count()):
        field = fields.nth(i)
        try:
            if field.locator("dt").inner_text().strip() == label:
                return field.locator("dd").inner_text().strip()
        except Exception:
            continue
    return None


def _read_trace_order_no(page: Page) -> str | None:
    """배송추적 페이지 하단 "주문정보"의 주문번호 (엉뚱한 주문인지 검증용)."""
    items = page.locator(INFO_ITEM_SELECTOR)
    for i in range(items.count()):
        item = items.nth(i)
        try:
            if item.locator("span.tit_prd").inner_text().strip() == ORDER_NO_LABEL:
                return item.locator("span.txt_prd").inner_text().strip()
        except Exception:
            continue
    return None


def _field_values(html: str) -> dict[str, str]:
    """배송추적 페이지 div.delivery_info 의 <dt>라벨</dt><dd>값</dd> 쌍."""
    return {label.strip(): _strip_tags(value)
            for label, value in re.findall(r"<dt>\s*([^<]+?)\s*</dt>\s*<dd>(.*?)</dd>", html, re.S)}


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _fetch_tracking_by_dlv_no(context: BrowserContext, dlv_no: str, order_no: str) -> tuple[str, str]:
    """배송추적 페이지를 HTML로 받아 (송장번호, 택배사)를 읽는다.

    이 페이지는 서버가 그려서 내려주므로(자바스크립트 필요 없음) 화면을 열지
    않고 context.request로 받는다 - 새 페이지를 열고 1.5초 자던 예전 방식
    (2.5초)이 0.3초로 준다 (2026-09-02 실측).
    """
    response = context.request.get(TRACE_URL.format(dlv_no=dlv_no))
    if "login.11st.co.kr" in response.url:
        raise BlockedError(f"배송추적 페이지에서 로그인이 풀렸습니다 (주문번호={order_no}).")
    html = response.text()

    info_items = [_strip_tags(x) for x in re.findall(r'<p class="prd_info"[^>]*>(.*?)</p>', html, re.S)]
    trace_order_no = next((item.replace(ORDER_NO_LABEL, "", 1).strip()
                           for item in info_items if item.startswith(ORDER_NO_LABEL)), None)
    if trace_order_no and trace_order_no != order_no:
        raise ParseError(
            f"배송추적 페이지의 주문번호({trace_order_no})가 조회하려던 주문번호({order_no})와 다릅니다."
        )

    fields = _field_values(html)
    raw_tracking = fields.get(TRACKING_FIELD_LABEL)
    if not raw_tracking:
        raise TrackingNotAvailableYet(
            f"배송추적 페이지에 아직 송장번호가 없습니다 (주문번호={order_no}, dlvNo={dlv_no})."
        )
    tracking_no = re.sub(r"[^0-9]", "", raw_tracking)
    if not tracking_no:
        raise ParseError(f"송장번호를 숫자로 읽지 못했습니다: {raw_tracking!r} (주문번호={order_no}).")

    raw_courier = fields.get(COURIER_FIELD_LABEL) or ""
    # "CJ대한통운 1588-1255" -> "CJ대한통운"
    courier_name = COURIER_PHONE_PATTERN.sub("", raw_courier).strip()
    courier = common.normalize_courier(courier_name) if courier_name else DEFAULT_COURIER
    return tracking_no, courier


def _collect_dlv_nos(page: Page) -> list[tuple[str, str]]:
    """주문상세의 "배송조회" 링크마다 (dlvNo, 그 링크가 속한 <tr>의 텍스트)를 모은다."""
    links = page.locator(TRACKING_LINK_SELECTOR)
    collected: list[tuple[str, str]] = []
    for i in range(links.count()):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        match = DLV_NO_PATTERN.search(href)
        if not match:
            continue
        try:
            row_text = link.locator("xpath=ancestor::tr[1]").inner_text()
        except Exception:
            row_text = ""
        collected.append((match.group(1), row_text))
    return collected


def _select_by_order_option(candidates: list[tuple[str, str]], order_option: str | None) -> str | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 상품 행(<tr>) 텍스트에만 유일하게
    나타나면 그 행의 dlvNo를 쓴다. 0개(표기가 안 맞음) 또는 2개 이상(애매함)
    매칭되면 None - 호출자가 전부 조회해서 비교하는 방식으로 넘어간다."""
    if len(candidates) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    matched = [dlv_no for dlv_no, row_text in candidates if target in normalize_option(row_text)]
    return matched[0] if len(matched) == 1 else None


def _scrape_tracking_from_page(
    context: BrowserContext, page: Page, order_no: str, order_option: str | None
) -> TrackingResult:
    candidates = _collect_dlv_nos(page)

    if not candidates:
        area_text = _order_area_text(page)
        normalized_area = normalize_option(area_text)
        if any(normalize_option(p) in normalized_area for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(area_text, order_no)
        raise ParseError(f"화면에서 배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    if len(candidates) == 1:
        tracking_no, courier = _fetch_tracking_by_dlv_no(context, candidates[0][0], order_no)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    matched_dlv_no = _select_by_order_option(candidates, order_option)
    if matched_dlv_no is not None:
        tracking_no, courier = _fetch_tracking_by_dlv_no(context, matched_dlv_no, order_no)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 조회해서 실제로 서로 다른 송장인지 확인한다
    # (다른 어댑터와 동일한 안전 규칙).
    results = [_fetch_tracking_by_dlv_no(context, dlv_no, order_no) for dlv_no, _ in candidates]
    if len({tracking_no for tracking_no, _ in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다."
        )

    tracking_no, courier = results[0]
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        _goto_logged_in(page, product_url)

        # 화면이 아직 덜 그려진 채로 읽으면 '아직 미발급'으로 잘못 넘길 수 있다
        # (조용히 틀리는 쪽이라 특히 위험하다). 그 주문의 주문번호가 화면에
        # 뜨면 다 그려진 것이다 - '배송조회' 같은 글자는 상단 메뉴에도 있어서
        # 표식으로 쓰면 덜 그려진 화면을 다 그려진 것으로 볼 수 있다.
        common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()


# ---------------------------------------------------------------------------
# 1:1 문의 남기기 / 답변 확인 (inquiry.py, inquiry_answers.py) - 사용자 요청 2026-09-16
# ---------------------------------------------------------------------------
# 사람이 하던 순서: 주문상세 → 왼쪽 메뉴 [주문/배송조회] → 그 주문의 [배송문의] → 팝업(내용
# 칸에 "주문번호 : N / 상품명 : ..."이 미리 적혀 있다)에 "○○○ 배송 언제 시작하나요?"를
# 이어 적고 [등록] → 왼쪽 메뉴 [상품 Q&A]에서 올라갔는지 확인.
#
# 실측(2026-09-16):
# - [배송문의]는 javascript:goQnaWrite(ordNo, prdNo, prdNm, isEmart) - 숨은 폼 forQnaFrm
#   (orderNo·prdNo·prdNm, acceptCharset euc-kr)을 새 창으로 POST해 상품문의 폼
#   (ProductQnaForm.tmall?method=insertProductQnAForm&...&qnaPathLoc=private)을 연다. 주문목록에
#   그 주문이 있어야 버튼이 보이고(배송중·배송준비중에만, 배송완료는 [판매자문의]뿐) 기본
#   목록은 최근 주문만 보여주므로, 여기서는 주문상세(product_url)에서 상품번호(a.product_info의
#   content-no)와 상품명을 읽어 같은 폼을 우리가 직접 POST한다 - 새 창을 잡을 필요도 없다.
# - 폼(frmMain)은 euc-kr 문서. 내용 textarea#brdInfoCont에 "주문번호 : N\n상품명 : ...\n"이
#   미리 들어 있고(사용자 지시: 그대로 두고 문구를 이어 적는다), 답변수신 메일은 채워져 있으며
#   [등록](#btnSave)은 내용 검사 → 개인정보 필터(동기 ajax, 걸리면 비밀글 alert) →
#   ProductQnaInsert.tmall?method=insertProductQnA 로 POST한다. 미리 적힌 문구에 주문번호가
#   들어 있어서 문의내역에서 우리 문의를 주문번호로 바로 찾을 수 있다.
# - [상품 Q&A](MyProductQnaAction.tmall?method=getMyProductQnaList)는 POST/GET 어느 쪽이든
#   startDate·endDate(YYYY/MM/DD)·answerStatus(ALL)·curPage로 10건씩 최신순, euc-kr 응답.
#   줄마다 상태(Icon_1 미답변 / Icon_2 답변완료)·viewContent('contentArea_i','문의번호')·
#   작성일, 접힌 영역 #contentArea_i에 질문(dl.question dt)과 답변(dl.answer 첫 dd 본문,
#   다음 dd "(답변일 : YYYY-MM-DD HH:MM)")이 있다. 전체 건수는 스크립트의
#   parseInt('N')(totalCount).
QNA_FORM_URL = ("https://www.11st.co.kr/product/ProductQnaForm.tmall?method=insertProductQnAForm"
                "&isSSL=Y&hostUrl=www.11st.co.kr&isSohoPrd=false&qnaPathLoc=private")
QNA_INSERT_URL_MARK = "ProductQnaInsert.tmall"
QNA_LIST_URL = "https://www.11st.co.kr/product/MyProductQnaAction.tmall?method=getMyProductQnaList"
QNA_CONTENT_SELECTOR = "#brdInfoCont"
QNA_SUBMIT_SELECTOR = "#btnSave"
QNA_PAGE_SIZE = 10
QNA_MAX_PAGES = 5            # 최대 50건 - 조회 기간을 주문일부터로 좁히므로 보통 한 쪽이다.
QNA_STEP_WAIT_MS = 10 * 1000
QNA_HISTORY_TRIES = 3        # 등록 직후 목록에 아직 없으면 잠깐 뒤 다시 본다.
QNA_HISTORY_RETRY_GAP_SEC = 1.5
PRODUCT_INFO_SELECTOR = "a.product_info[ord-no='{order_no}']"
QNA_ORDER_NO_PATTERN = re.compile(r"주문번호\s*:\s*(\d+)")
QNA_TOTAL_PATTERN = re.compile(r"var totalCount\s*=\s*parseInt\('(\d+)'\)")
QNA_ROW_PATTERN = re.compile(
    r'<span class="Icon_\d">\s*<span>\s*(?P<state>[^<]*?)\s*</span>\s*</span>.*?'
    r"viewContent\('contentArea_(?P<idx>\d+)','(?P<id>\d+)'.*?</button>.*?"
    r"<td><p>(?P<written>\d{4}-\d{2}-\d{2} \d{2}:\d{2})</p></td>", re.S)
QNA_QUESTION_PATTERN = re.compile(r'<dl class="question">\s*<dt>(.*?)</dt>', re.S)
QNA_ANSWER_BLOCK_PATTERN = re.compile(r'<dl class="answer">(.*?)</dl>', re.S)
QNA_ANSWER_TEXT_PATTERN = re.compile(r"<dd>(.*?)</dd>", re.S)
QNA_ANSWER_DATE_PATTERN = re.compile(r"답변일\s*:\s*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)")


def _inquiry_text(recipient_name: str) -> str:
    return f"{recipient_name.strip()} 배송 언제 시작하나요?"


def _goto_logged_in(page: Page, url: str) -> None:
    """주소를 열고, 로그인 화면이면 자동 로그인한 뒤 다시 연다 (get_tracking과 같은 경로)."""
    page.goto(url, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        common.safe_print("[11st] 로그인 세션이 없어 자동 로그인을 시도합니다.")
        if not _auto_login(page):
            raise BlockedError("11번가 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
        page.goto(url, wait_until="domcontentloaded")
        if _looks_like_login_page(page):
            raise BlockedError("11번가 로그인 후에도 여전히 로그인 페이지입니다.")


def _fetch_qna_page(context: BrowserContext, since: date, page_no: int) -> str | None:
    """[상품 Q&A] 목록 한 쪽(HTML, euc-kr을 푼 것). 로그인이 풀렸거나 못 읽으면 None."""
    try:
        response = context.request.post(QNA_LIST_URL, form={
            "flag": "myPrdQna", "curPage": str(page_no), "memNo": "0", "answerStatus": "ALL",
            "searchGubun": "01", "searchTxt": "",
            "startDate": since.strftime("%Y/%m/%d"), "endDate": date.today().strftime("%Y/%m/%d"),
        })
    except Exception:  # noqa: BLE001 - 통신 실패는 '못 읽음'
        return None
    if not response.ok or "login.11st.co.kr" in response.url:
        return None
    html = response.body().decode("cp949", "replace")
    return html if 'name="frmMain"' in html else None


def _qna_rows(html: str) -> list[dict]:
    """목록 HTML의 줄마다 {inquiry_id, state, written_at, question, order_no, answer, answered_on}."""
    rows: list[dict] = []
    for m in QNA_ROW_PATTERN.finditer(html):
        block_m = re.search(rf'<div id="contentArea_{m.group("idx")}".*?</td>', html, re.S)
        block = block_m.group(0) if block_m else ""
        q = QNA_QUESTION_PATTERN.search(block)
        question = common.html_to_text(q.group(1)) if q else ""
        answer = answered_on = None
        a = QNA_ANSWER_BLOCK_PATTERN.search(block)
        if a:
            dds = QNA_ANSWER_TEXT_PATTERN.findall(a.group(1))
            answer = common.html_to_text(dds[0]) if dds else None
            d = QNA_ANSWER_DATE_PATTERN.search(a.group(1))
            answered_on = d.group(1) if d else None
        o = QNA_ORDER_NO_PATTERN.search(question)
        rows.append({
            "inquiry_id": m.group("id"),
            "state": m.group("state").strip(),
            "written_at": datetime.strptime(m.group("written"), "%Y-%m-%d %H:%M"),
            "question": question,
            "order_no": o.group(1) if o else None,
            "answer": answer or None,
            "answered_on": answered_on,
        })
    return rows


def _same_message(question: str, message: str) -> bool:
    return re.sub(r"\s+", "", message) in re.sub(r"\s+", "", question)


def _find_listed_qna(context: BrowserContext, order_no: str, message: str, since: date, *,
                     max_pages: int = QNA_MAX_PAGES, inquiry_id: str | None = None,
                     first_html: str | None = None) -> dict | None:
    """[상품 Q&A]에서 since 이후에 쓴, 이 주문번호가 적힌 우리 문구의 문의를 찾는다 (최신순).

    문의번호(inquiry_id)가 있으면 그 줄이면 바로 맞는다. 목록을 못 읽으면 ParseError -
    모르는 채로 등록하지 않는다.
    """
    for page_no in range(1, max_pages + 1):
        html = first_html if (page_no == 1 and first_html is not None) else _fetch_qna_page(context, since, page_no)
        if html is None:
            raise ParseError("[상품 Q&A] 목록을 읽지 못했습니다 (로그인 세션이 없거나 화면이 바뀜).")
        rows = _qna_rows(html)
        if not rows:
            return None
        for row in rows:
            if row["written_at"].date() < since:
                return None
            if (inquiry_id and row["inquiry_id"] == inquiry_id) or (
                    row["order_no"] == order_no and _same_message(row["question"], message)):
                return row
        total_m = QNA_TOTAL_PATTERN.search(html)
        total = int(total_m.group(1)) if total_m else 0
        if page_no * QNA_PAGE_SIZE >= total:
            return None
    return None


def _describe_listed(row: dict) -> str:
    return f"{row['state'] or '상태 모름'} {row['written_at']:%Y-%m-%d %H:%M} (문의번호 {row['inquiry_id']})"


def _confirm_qna_listed(context: BrowserContext, order_no: str, message: str) -> str:
    """[상품 Q&A] 첫 쪽에 이 주문의 우리 문의가 오늘 자로 올라갔는지 확인한다."""
    for attempt in range(1, QNA_HISTORY_TRIES + 1):
        found = _find_listed_qna(context, order_no, message, date.today(), max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < QNA_HISTORY_TRIES:
            common.sleep(QNA_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"[등록]을 눌렀지만 [상품 Q&A]에서 확인되지 않았습니다. 다시 남기기 전에 11번가 "
        f"나의 11번가 → 상품 Q&A에 '{message}'가 있는지 직접 확인해주세요.")


def _order_date_of(order_no: str) -> date:
    """주문번호 앞 8자리가 주문일(YYYYMMDD) - 20260915101097648은 2026-09-15 주문."""
    try:
        return datetime.strptime(order_no[:8], "%Y%m%d").date()
    except ValueError:
        raise ParseError(f"주문번호에서 주문일을 읽을 수 없습니다 (ordNo={order_no}).") from None


ORDER_ITEMS_START = "주문상품정보"
ORDER_ITEMS_END = "배송지 정보"


def _order_items_text(area_text: str) -> str:
    """주문상세 본문에서 '주문 상품 정보' 표(번호·상품/옵션정보·…·주문/배송상태) 구간만."""
    start = area_text.find(ORDER_ITEMS_START)
    end = area_text.find(ORDER_ITEMS_END, start if start >= 0 else 0)
    if start < 0:
        return area_text
    return area_text[start:end] if end > start else area_text[start:]


def _read_product(page: Page, order_no: str) -> tuple[str, str]:
    """주문상세의 상품 링크(a.product_info)에서 (상품번호, 상품명)."""
    link = page.locator(PRODUCT_INFO_SELECTOR.format(order_no=order_no)).first
    try:
        link.wait_for(state="attached", timeout=common.ORDER_RENDER_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError(f"주문상세에서 상품 정보를 찾지 못했습니다 (ordNo={order_no}).") from None
    prd_no = (link.get_attribute("content-no") or "").strip()
    prd_nm = next((ln.strip() for ln in link.inner_text().splitlines() if ln.strip()), "")
    if not prd_no or not prd_nm:
        raise ParseError(f"주문상세의 상품번호/상품명을 읽지 못했습니다 (ordNo={order_no}, prdNo={prd_no!r}).")
    return prd_no, prd_nm


OPEN_QNA_FORM_JS = """([url, orderNo, prdNo, prdNm]) => {
    const f = document.createElement('form');
    f.method = 'post'; f.action = url; f.acceptCharset = 'euc-kr';
    for (const [k, v] of [['orderNo', orderNo], ['prdNo', prdNo], ['prdNm', prdNm]]) {
        const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
    }
    document.body.appendChild(f); f.submit();
}"""


def _open_qna_form(page: Page, order_no: str, prd_no: str, prd_nm: str) -> str:
    """[배송문의]가 여는 상품문의 폼을 같은 탭에 열고, 미리 적힌 내용을 돌려준다."""
    with page.expect_navigation(wait_until="domcontentloaded", timeout=QNA_STEP_WAIT_MS):
        page.evaluate(OPEN_QNA_FORM_JS, [QNA_FORM_URL, order_no, prd_no, prd_nm])
    if _looks_like_login_page(page):
        raise BlockedError("상품문의 폼을 여는데 로그인 화면으로 넘어갔습니다.")
    try:
        page.locator(QNA_CONTENT_SELECTOR).wait_for(state="visible", timeout=QNA_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError(f"상품문의 폼이 뜨지 않았습니다 (url={page.url}).") from None
    prefilled = page.locator(QNA_CONTENT_SELECTOR).input_value()
    if order_no not in prefilled:
        raise ParseError(f"상품문의 폼에 미리 적힌 내용에 이 주문번호가 없습니다 (ordNo={order_no}, 내용={prefilled!r}).")
    return prefilled


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """[배송문의] 폼에 "○○○ 배송 언제 시작하나요?"를 이어 적어 [등록]하고, [상품 Q&A]에서 확인한 문구를 돌려준다.

    주문상세에서 취소/품절이면 남기지 않고, 받는사람이 수령인과 다르면 엉뚱한 주문이라
    멈춘다. [상품 Q&A]에 이 주문의 같은 문의가 주문일 이후에 이미 있으면 AlreadyInquired.
    미리 적힌 "주문번호 : N / 상품명 : ..."은 그대로 두고 그 아래 줄에 문구를 적는다(사용자
    지시). 등록 뒤 [상품 Q&A] 첫 쪽에 오늘 자로 올라갔는지 확인한다.
    """
    order_no = extract_order_no(product_url)
    message = _inquiry_text(recipient_name)
    order_date = _order_date_of(order_no)
    page = context.new_page()
    try:
        _goto_logged_in(page, product_url)
        if not common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS):
            raise ParseError(f"주문상세가 그려지지 않았습니다 (ordNo={order_no}).")
        area_text = _order_area_text(page)
        # 취소/품절 판정은 '주문 상품 정보' 표 구간만 본다 - 왼쪽 메뉴의 "취소/반품/교환 신청"과
        # 안내문의 "준비"가 본문 전체에는 늘 있어 raise_if_cancelled가 헛짚는다(2026-09-16 실측).
        raise_if_cancelled(_order_items_text(area_text), order_no)
        if recipient_name.strip() and recipient_name.strip() not in area_text:
            raise ParseError(f"주문상세의 받는사람이 수령인 '{recipient_name}'과 다릅니다 (ordNo={order_no}).")
        prd_no, prd_nm = _read_product(page, order_no)

        existing = _find_listed_qna(context, order_no, message, order_date)
        if existing is not None:
            raise AlreadyInquired(f"[상품 Q&A]에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

        prefilled = _open_qna_form(page, order_no, prd_no, prd_nm)
        content = prefilled if prefilled.endswith("\n") else prefilled + "\n"
        content += message
        box = page.locator(QNA_CONTENT_SELECTOR)
        box.fill(content)
        if box.input_value() != content:
            raise ParseError("문의 내용이 입력되지 않았습니다.")

        dialogs: list[tuple[str, str]] = []

        def _on_dialog(dialog) -> None:
            dialogs.append((dialog.type, dialog.message))
            dialog.accept()   # 개인정보 탐지 안내(비밀글 전환)·완료 안내 모두 닫는다

        page.on("dialog", _on_dialog)
        page.locator(QNA_SUBMIT_SELECTOR).click()
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            page.wait_for_url(lambda url: QNA_INSERT_URL_MARK in url, wait_until="commit",
                              timeout=QNA_STEP_WAIT_MS)
        # 등록 응답이 alert이나 화면 글자로 오면 사유에 같이 적는다(없어도 목록 확인이 기준).
        with contextlib.suppress(PlaywrightError):
            page.wait_for_load_state("domcontentloaded", timeout=QNA_STEP_WAIT_MS)
        blocked = [m for t, m in dialogs if "입력" in m and ("불가" in m or "하세요" in m)]
        if blocked:
            raise ParseError(f"[등록]이 거부되었습니다: {blocked[0]}")
        listed = _confirm_qna_listed(context, order_no, message)
        # 실측 alert 두 개: "상품 Q&A에 개인정보로 탐지되는 내용이 포함되어 있어 비밀글로 게시됩니다.
        # 개인정보 탐지 항목:이름," 그리고 "등록 완료 되었습니다." - 완료 문구만 앞에 싣고 비밀글은 표시만.
        done = next((m.strip().splitlines()[0] for _, m in dialogs if "완료" in m), "등록")
        secret = " (비밀글)" if any("비밀글" in m for _, m in dialogs) else ""
        return f"{done}{secret} · 상품 Q&A 확인: {listed}"
    finally:
        page.close()


def fetch_inquiry_answer(context: BrowserContext, product_url: str, recipient_name: str, *,
                         since: date, inquiry_id: str | None = None, headless: bool = False) -> dict | None:
    """이 주문에 남긴 상품 Q&A의 상태·답변 - {inquiry_id, state, written_on, answer, answered_on}, 없으면 None.

    [상품 Q&A] 목록(request)으로 세션을 확인하고(없으면 화면을 열어 자동 로그인) 주문일부터의
    목록에서 이 주문번호가 적힌 우리 문구의 줄을 찾는다 - 목록에 답변까지 같이 오므로
    상세를 열 일이 없다.
    """
    order_no = extract_order_no(product_url)
    message = _inquiry_text(recipient_name)
    html = _fetch_qna_page(context, since, 1)
    if html is None:
        page = context.new_page()
        try:
            _goto_logged_in(page, QNA_LIST_URL)
        finally:
            page.close()
        html = _fetch_qna_page(context, since, 1)
        if html is None:
            raise BlockedError("11번가 [상품 Q&A]를 읽지 못했습니다 (로그인 세션이 없습니다).")
    found = _find_listed_qna(context, order_no, message, since, inquiry_id=inquiry_id, first_html=html)
    if found is None:
        return None
    return {
        "inquiry_id": found["inquiry_id"],
        "state": found["state"],
        "written_on": f"{found['written_at']:%Y-%m-%d}",
        "answer": found["answer"],
        "answered_on": (found["answered_on"] or "")[:10] or None,
    }
