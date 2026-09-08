"""SSG.COM 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo=<주문번호(하이픈 제거)>
- 롯데온/지마켓과 달리 "배송조회" 버튼을 누를 필요가 없다 - "배송상세현황
  보기" 바로 다음 줄에 "택배사 / 송장번호 상태문구" 형태로 이미 렌더링되어
  있다 (예: "CJ대한통운 / 585642147431 배송완료"). 상태문구는 배송중/
  배송출발/배송완료 등 여러 값이 나올 수 있어(처음엔 배송중·배송완료만
  보고 정규식에 그 둘만 하드코딩했다가 배송출발 상태인 주문을 전부
  "미발급"으로 잘못 스킵한 적이 있다) 상태문구 자체는 매칭하지 않고
  "택배사 / 숫자" 패턴만 본다. 아직 발송 전이면 이 자리에 "판매자에게
  주문이 전달되었습니다." 같은 상태 문구만 있고 송장 패턴이 없다.
- 로그인이 풀려 있으면 이 URL이 그대로 member.ssg.com/member/login.ssg로
  리다이렉트되고, 로그인 폼은 롯데온/지마켓처럼 팝업이 아니라 같은 탭에 뜬다.
  로그인 성공 시 로그인 폼의 retURL 파라미터로 원래 페이지로 자동
  복귀하는 것까지 확인했다.
- 사용자가 SSG는 쿠키(storage_state) 기반 자동 로그인에 더해, 세션이
  끊겼을 때도 사람 개입 없이 완전 자동으로 재로그인되길 원했다 (요청 사항).
  롯데온/지마켓은 보안상 비밀번호를 절대 자동 입력하지 않도록 만들었지만,
  SSG는 명시적으로 요청받아 SSG_ID/SSG_PW 환경변수로 완전 자동 로그인한다.
  두 사이트와 다른 이 사이트만의 예외이니 다른 어댑터에 이 패턴을
  그대로 옮기지 말 것.
- 주문목록(pay.ssg.com/myssg/orderInfo.ssg?page=N, 10건씩, 최근 3개월)이
  주문마다 상세와 **완전히 같은 표기**("배송상세현황 보기" 다음 줄의
  "택배사 / 송장번호 상태", 미발급 상태 문구, "주문취소완료", 옵션, 주문일,
  출고예정)를 통째로 보여준다(2026-09-02 실측). 그래서 prepare_batch가 목록
  몇 페이지를 읽어두면 성공/미발급/취소 전부 상세를 열지 않고 결론이 난다 -
  주문마다 상세 페이지를 열던 것(건당 1~2초 + 요청 간격)이 페이지 몇 번으로
  끝난다. 목록에 없는 주문(3개월보다 오래됨 등)만 예전처럼 상세를 연다.
"""

from __future__ import annotations

import contextlib
import html as html_mod
import os
import re
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import eta as eta_mod
from .. import order_date as order_date_mod
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
    raise_if_cancelled,
    normalize_option,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#mem_id"
LOGIN_PW_SELECTOR = "#mem_pw"

DOMAINS = {"ssg.com", "www.ssg.com", "pay.ssg.com"}
SITE_KEY = "ssg"

LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초

TRACKING_ANCHOR = "배송상세현황 보기"
TRACKING_LINE_PATTERN = re.compile(r"([가-힣A-Za-z]{2,10})\s*/\s*([0-9][0-9\-]{7,})")
NOT_YET_PATTERNS = [
    "전달되었습니다",
    "시작하였습니다",
    "시작되었습니다",
    "결제완료",
    "상품준비중",
    "배송준비중",
]
# 취소/품절이 확정된 주문에만 찍히는 표시. 진행 중 주문은 화면에 진행 단계
# 라벨("결제완료 / 상품준비중 / 배송준비중")이 항상 같이 찍혀 있어 NOT_YET
# 판정이 먼저 이기므로, 이 표시는 NOT_YET 판정보다 **먼저** 본다. 2026-09-03
# 실측: 품절 처리 중인 주문이 "상품품절" 표시와 진행 단계 라벨을 함께 보여줘
# '아직 미발급'으로 넘어갔다 (환불이 끝난 뒤에야 "주문취소완료"로 바뀌고
# 라벨이 사라진다). '준비' 우선 규칙(raise_if_cancelled)도 여기엔 안 쓴다 -
# 그 라벨이 바로 '준비'라서 규칙을 적용하면 같은 이유로 또 놓친다.
CANCELLED_MARKERS = ["주문취소완료", "취소완료", "상품품절", "품절취소"]

# --------------------------------------------------------------------------
# 주문목록 한 번으로 여러 건 답하기 (prepare_batch)
# --------------------------------------------------------------------------
# 주문목록은 주문마다 상세와 같은 표기("택배사 / 송장번호 상태", 미발급 문구,
# "주문취소완료", 옵션, 출고예정)를 통째로 보여준다(2026-09-02 실측: 성공/
# 미발급 주문의 송장·상태가 상세와 글자까지 동일). 그래서 목록의 주문 구간
# 텍스트를 상세 화면 텍스트 대신 그대로 파서에 넣는다 - 파서가 같으니 결론도
# 같다. 페이지당 10건, 기본 조회기간은 최근 3개월이다.
ORDER_LIST_URL = "https://pay.ssg.com/myssg/orderInfo.ssg?viewType=Ssg&page={page}"
LIST_MAX_PAGES = 5          # 10건씩 -> 최대 50건. 못 덮은 주문은 상세 폴백으로.
LIST_PREFETCH_MIN_ORDERS = 2  # 1건이면 목록이나 상세나 페이지 하나라 이득이 없다.
# 목록의 주문 구간 머리: "2026.09.01 주문번호 20260901-6F46A2" (한 줄).
# 주문번호에서 하이픈을 빼면 상세 URL의 orordNo와 같다.
LIST_SECTION_PATTERN = re.compile(r"\d{4}\.\d{2}\.\d{2}\s*주문번호\s*([0-9]{8}-[0-9A-F]{4,10})")
# 마지막 주문 구간의 끝 - 이 밑으로는 페이지네이션/FAQ 꼬리라, 꼬리의
# "주문취소" 같은 글자가 마지막 주문의 판정에 섞이지 않게 잘라낸다.
LIST_TAIL_MARKERS = ("\n처음", "주문에 불편함이 있으신가요")

# prepare_batch가 읽어둔 {주문번호(orordNo): 목록의 그 주문 구간 텍스트}.
# 컨텍스트(=이번 실행의 브라우저)별로 담는다 (롯데온/29CM와 동일).
_listed_orders: dict[int, dict[str, str]] = {}


def _split_list_sections(body_text: str) -> dict[str, str]:
    """주문목록 화면 텍스트를 주문번호별 구간으로 쪼갠다."""
    matches = list(LIST_SECTION_PATTERN.finditer(body_text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body_text)
        section = body_text[m.start():end]
        if i + 1 == len(matches):  # 마지막 구간만 페이지 꼬리가 붙는다
            for marker in LIST_TAIL_MARKERS:
                cut = section.find(marker)
                if cut != -1:
                    section = section[:cut]
        sections[m.group(1).replace("-", "")] = section
    return sections


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문들을 주문목록 페이지로 미리 통째로 읽어둔다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 실패하면(세션
    만료 포함) 아무것도 읽지 않은 것과 같아서 모든 주문이 예전처럼 상세
    경로로 간다 - 그래서 어떤 예외도 밖으로 내보내지 않는다.
    """
    wanted = set()
    for order in orders:
        try:
            wanted.add(extract_order_no(order.product_url))
        except ParseError:
            continue  # 이런 주문은 어차피 상세 경로에서 같은 이유로 실패한다
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return

    page = context.new_page()
    try:
        found: dict[str, str] = {}
        for page_no in range(1, LIST_MAX_PAGES + 1):
            page.goto(ORDER_LIST_URL.format(page=page_no), wait_until="domcontentloaded")
            if page_no == 1 and _looks_like_login_page(page):
                # 세션이 만료됐으면 여기서 한 번 로그인해둔다 - 실패하면 조용히
                # 물러나고, 사유는 상세 경로의 로그인 시도가 주문별로 남긴다.
                if not _auto_login(page):
                    return
                page.goto(ORDER_LIST_URL.format(page=page_no), wait_until="domcontentloaded")
            sections = _split_list_sections(page.inner_text("body"))
            if not sections:
                break  # 목록의 끝(빈 페이지) - 못 찾은 건은 상세 폴백으로
            found.update(sections)
            if not (wanted - found.keys()):
                break
        _listed_orders[id(context)] = found
        common.safe_print(
            f"[ssg] 주문목록에서 {len(wanted & found.keys())}/{len(wanted)}건을 미리 읽었습니다.")
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 그냥 상세 경로로 간다
        common.safe_print(f"[ssg] 주문목록을 읽지 못해 주문마다 상세 화면을 엽니다 ({e}).")
    finally:
        page.close()


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("orordNo")
    if not values:
        raise ParseError(f"URL에서 orordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page) -> bool:
    return common.looks_like_login_page(
        page, lambda url: "member.ssg.com" in url or "login" in url.lower())


def _auto_login(page) -> bool:
    """SSG_ID/SSG_PW로 완전 자동 로그인한다 (사용자 명시 요청 - 다른 사이트와 다름)."""
    ssg_id = os.environ.get("SSG_ID")
    ssg_pw = os.environ.get("SSG_PW")
    if not ssg_id or not ssg_pw:
        raise BlockedError(
            "SSG 로그인이 필요하지만 SSG_ID/SSG_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    page.fill(LOGIN_ID_SELECTOR, ssg_id)
    page.fill(LOGIN_PW_SELECTOR, ssg_pw)
    page.get_by_role("button", name="로그인", exact=True).first.click()

    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500
    return False


def _select_by_order_option(body_text: str, anchor_matches: list[tuple[int, re.Match]], order_option: str | None):
    """샵마인 엑셀의 "주문옵션" 값이 어느 앵커("배송상세현황 보기") 바로
    앞(보통 상품명/옵션은 앵커보다 앞에 나온다) 텍스트에만 유일하게
    나타나면 그 매치를 쓴다. 0개(표기가 안 맞음) 또는 2개 이상(애매함)
    매칭되면 None - 호출자가 기존 방식(사람 확인 요청)으로 넘어간다."""
    if len(anchor_matches) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    candidates = []
    prev_end = 0
    for anchor_pos, m in anchor_matches:
        # window 시작을 이전 상품 구간 끝 이후로 묶어서, 앞 상품의 옵션
        # 텍스트가 다음 상품 판단에 섞여 들어가지(bleed) 않게 한다.
        window = body_text[max(prev_end, anchor_pos - 400) : anchor_pos]
        if target in normalize_option(window):
            candidates.append(m)
        prev_end = anchor_pos + m.end()  # 이 구간 안에서의 매치 끝을 절대 위치로 환산
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page, order_no: str, order_option: str | None = None) -> TrackingResult:
    return _tracking_from_text(page.inner_text("body"), order_no, order_option)


def _tracking_from_text(body_text: str, order_no: str, order_option: str | None = None) -> TrackingResult:
    """주문 하나 분량의 화면 텍스트로 결론을 낸다.

    상세 화면 전체를 넣든 주문목록의 그 주문 구간만 넣든 같은 표기라 같은
    결론이 난다 (prepare_batch 주석 참고).
    """
    # "택배사 / 숫자" 패턴이 본문 다른 곳(사업자번호 등)에서도 우연히 매칭될
    # 가능성을 줄이기 위해 "배송상세현황 보기" 바로 뒤 구간만 본다. 이 앵커가
    # 상품별로 여러 번 나오면(상품별로 나눠 배송된 주문) 각각의 구간을 모두 본다.
    anchor_positions = [m.start() for m in re.finditer(re.escape(TRACKING_ANCHOR), body_text)]
    windows = [body_text[pos : pos + 120] for pos in anchor_positions] if anchor_positions else [body_text]

    anchor_matches: list[tuple[int, re.Match]] = []
    for pos, window in zip(anchor_positions or [0], windows):
        m = TRACKING_LINE_PATTERN.search(window)
        if m:
            anchor_matches.append((pos, m))

    if not anchor_matches:
        marker = next((m for m in CANCELLED_MARKERS if m in body_text), None)
        if marker:
            raise OrderCancelled(
                f"주문 화면에 '{marker}' 표시가 있습니다 (orordNo={order_no}) - "
                "취소/품절 주문인지 확인해주세요."
            )
        combined = "\n".join(windows)
        if any(p in combined for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orordNo={order_no}).")
        # 취소/품절은 "배송상세현황 보기" 주변 구간(combined)이 아니라 화면
        # 전체에 표시되므로 body_text를 본다.
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"화면에서 송장번호 텍스트를 찾지 못했습니다 (orordNo={order_no}).")

    distinct_tracking_nos = {re.sub(r"[^0-9]", "", m.group(2)) for _, m in anchor_matches}
    tracking_match = _select_by_order_option(body_text, anchor_matches, order_option)
    if tracking_match is None:
        if len(distinct_tracking_nos) > 1:
            # 한 주문이 상품별로 나눠 배송되어 서로 다른 송장번호가 여러 개
            # 보이는 경우다 - 어느 걸 써야 하는지 확신할 수 없어 사람이
            # 확인하게 한다 (무신사 어댑터와 동일한 안전 규칙).
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (orordNo={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
        tracking_match = anchor_matches[0][1]

    courier = common.normalize_courier(tracking_match.group(1).strip())
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def _answer_from_section(section: str, order_no: str, order_option: str | None) -> TrackingResult:
    """미리 읽어둔 주문목록 구간으로 답한다 - 요청을 안 보냈다는 표시를 싣는다."""
    try:
        result = attach_order_date(
            order_date_mod.from_text(section),
            lambda: _tracking_from_text(section, order_no, order_option),
            delivery_note=eta_mod.from_text(section),
        )
    except AdapterError as e:
        e.sent_request = False
        raise
    result.sent_request = False
    return result


def _goto_logged_in(page, url: str) -> None:
    """이 주소를 연다 - 로그인 화면으로 넘어가면 자동 로그인하고 다시 연다.

    송장조회(get_tracking)와 문의 남기기(post_inquiry)가 같은 로그인 처리를 쓴다.
    로그인 폼의 retURL로 원래 주소에 돌아오지만(맨 위 docstring), 확실히 하려고
    한 번 더 연다.
    """
    page.goto(url, wait_until="domcontentloaded")
    if not _looks_like_login_page(page):
        return
    common.safe_print("[ssg] 로그인 세션이 없어 자동 로그인을 시도합니다.")
    if not _auto_login(page):
        raise BlockedError("SSG 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
    if _looks_like_login_page(page):
        raise BlockedError("SSG 로그인 후에도 여전히 로그인 페이지입니다.")
    page.goto(url, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        raise BlockedError("SSG 로그인 후에도 여전히 로그인 페이지입니다.")


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)

    # 주문목록에서 이미 통째로 읽어둔 주문이면 요청 없이 여기서 끝낸다
    # (prepare_batch). 구간을 해석할 수 없을 때(ParseError)만 예전처럼 상세를
    # 연다 - 목록과 상세의 레이아웃이 미묘하게 다른 주문일 수 있어서, 확실한
    # 결론(성공/미발급/취소)만 목록으로 답한다.
    section = _listed_orders.get(id(context), {}).get(order_no)
    if section is not None:
        try:
            return _answer_from_section(section, order_no, order_option)
        except ParseError:
            pass  # 상세 폴백

    page = context.new_page()
    try:
        _goto_logged_in(page, product_url)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        page.close()


# --------------------------------------------------------------------------
# E-mail 상담 남기기 (post_inquiry) - 2026-09-08 실측
# --------------------------------------------------------------------------
# 주문일이 이틀 지나도록 안 나간 주문에 "○○○ 배송 언제 시작하나요?"를 남긴다
# (inquiry.py). 사람이 누르는 순서 그대로다:
#   주문상세 왼쪽 메뉴 [E-mail 상담] -> www.ssg.com/customer/counselForm.ssg
#     * 주문과 무관한 고정 주소다(롯데온처럼 주문번호가 주소에 붙지 않는다).
#       그래서 주문상세를 열 필요가 없고 - 취소/품절 여부만 주문목록(prepare_batch가
#       읽어둔 구간)이나 상세 화면으로 본 뒤 바로 이 주소로 간다.
#   유형 대분류 [배송](select#priorWebCnslClsNo=Q89) -> 중분류가 ajax
#     (listWebCnslCls.ssg)로 채워진다 -> [배송 일정 확인](select#webCnslClsNo=Q90)
#     * 두 select는 display:none이고 위에 템플릿 드롭다운 UI가 덮여 있다. 값은
#       jQuery로 넣고 change(인라인 onchange가 ajax를 부른다)와 sync(UI 글자 갱신)를
#       올린다. 유형을 고르면 내용 칸이 비어 있을 때 안내 템플릿(cnslTemplCntt.ssg,
#       "※ 빠른 처리를 위해 ...")을 받아 **응답이 오는 대로 내용 칸을 덮어쓴다** -
#       그래서 내용은 이 응답을 받은 뒤에 넣는다.
#   [주문상품 선택](#btnOrdItemList) -> 레이어 #ccs_sel_prod1 에 최근 1개월 주문이
#     ajaxOrdListForMyssgCs.ssg(HTML, 페이지 없음 - 2026-09-08 실측 184건 한 번에)로
#     채워진다 -> 이 주문의 상품 checkbox(#ordItem_<주문번호>_<순번>) 체크 -> [확인]
#     (setItem) -> 폼 아래 #itemSelected 에 상품이 그려지고 숨은 값
#     cnslItemBaseDto.ordNo/ordItemSeqs 에 주문번호·순번이 들어간다.
#     * 1개월 목록에 없으면(주문일이 한 달을 넘긴 건) 주문일 하루짜리 조회
#       (searchDtType=0&startDate=&endDate=, 'YYYY.MM.DD')로 다시 채운다.
#     * 상품의 itemId(#itemId<주문번호>_<순번>)를 여기서 읽어둔다 - 문의내역에는
#       주문번호가 없고 상품(itemId)만 있어 이 주문의 문의인지 그것으로 맞춘다.
#   제목(#cnslDemndTitleNm, 30자)·내용(#cnslDemndCntt) 모두 우리 문구 -> 답변방법은
#     기본값 SMS(#reply03)·알림요청 체크 그대로 -> [등록](#regBtn)
#     -> confirm("010-****-**** 의 연락처로 문자 답변을 받으시겠습니까?")
#     -> POST counselCreate.ssg (JSON resultCode SUCCESS)
#     -> alert("문의가 등록되었습니다. ...") -> E-mail 답변확인(counselList.ssg)으로 이동
#   validateForm이 유형·제목·내용·상품(배송은 needOrdNo)이 비면 alert로 막는다 -
#   그 alert도 dialog 핸들러로 받아 실패 사유에 싣는다.
# 문의내역 확인: [E-mail 답변확인](myssg/activityMng/counselList.ssg, 10건씩,
#   ?page=N)이 <li onclick="showCounselContent(<문의번호>)"> 줄마다 제목(우리 문구
#   그대로)과 상태(처리중/답변완료)를 준다. 날짜·주문번호는 없고, 펼치면 부르는
#   상세(counselDetail.ssg?cnslId=, HTML 조각)에 작성일("2026.09.08 10:50")·제목·
#   상품(itemView.ssg?itemId=...)·답변이 있다. 상세는 GET이고 부작용이 없다.
#   지마켓처럼 **등록 전**에 같은 제목의 문의를 상세까지 열어 이 주문 상품(itemId)의
#   것이 주문일 이후에 있으면 AlreadyInquired로 넘긴다(2026-09-08 실측: 사용자가
#   예시로 준 주문은 이미 그날 10:50에 직접 남겨 '처리중'이었다). **등록 후**에는
#   오늘 자로 올라갔는지 확인해 완료 문구에 붙인다.
COUNSEL_FORM_URL = "https://www.ssg.com/customer/counselForm.ssg"
COUNSEL_LIST_URL = "https://www.ssg.com/myssg/activityMng/counselList.ssg?page={page}&menu=counselList"
COUNSEL_DETAIL_URL = "https://www.ssg.com/myssg/activityMng/counselDetail.ssg?cnslId={cnsl_id}"
INQUIRY_TYPE_SELECT = "#priorWebCnslClsNo"
INQUIRY_SUBTYPE_SELECT = "#webCnslClsNo"
INQUIRY_TYPE = "배송"
INQUIRY_SUBTYPE = "배송 일정 확인"
INQUIRY_SUBTYPE_API = "listWebCnslCls.ssg"      # 대분류를 고르면 중분류 목록을 주는 응답
INQUIRY_TEMPLATE_API = "cnslTemplCntt.ssg"      # 유형을 고르면 내용 칸을 덮어쓰는 안내 템플릿
INQUIRY_ITEM_BUTTON = "#btnOrdItemList"         # [주문상품 선택]
INQUIRY_ITEM_LAYER = "#ccs_sel_prod1"
INQUIRY_ITEM_TABLE = "#tbOrdItem"
INQUIRY_ITEM_LIST_API = "ajaxOrdListForMyssgCs.ssg"
INQUIRY_ITEM_CONFIRM = f"{INQUIRY_ITEM_LAYER} .btn_submit"   # 레이어의 [확인]
INQUIRY_ITEM_SELECTED = "#itemSelected"
INQUIRY_TITLE = "#cnslDemndTitleNm"
INQUIRY_TITLE_MAX = 30
INQUIRY_CONTENT = "#cnslDemndCntt"
INQUIRY_SUBMIT = "#regBtn"
INQUIRY_DONE_TEXT = "문의가 등록되었습니다"
INQUIRY_DONE_URL = "counselList"
INQUIRY_MESSAGE = "{name} 배송 언제 시작하나요?"
INQUIRY_STEP_WAIT_MS = 10000   # 클릭 뒤 다음 화면 요소·응답이 오기까지 최대
INQUIRY_HISTORY_TRIES = 3      # 등록 뒤 목록에 아직 안 보이면 이만큼 다시 본다
INQUIRY_HISTORY_RETRY_GAP_SEC = 1.0
INQUIRY_HISTORY_MAX_PAGES = 5  # '이미 남겼는지' 훑는 문의내역 페이지 수 (10건씩)
# 문의 화면에서 받아줄 호스트 - 화면·API·정적파일·로그인이 이 안이다. 나머지는
# 광고/분석 태그(moloco·criteo·amplitude·google 등, 2026-09-08 실측 20여 곳)라 끊는다.
INQUIRY_ALLOWED_HOSTS = ("ssg.com", "ssgcdn.com")
INQUIRY_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
COUNSEL_ROW_PATTERN = re.compile(r'showCounselContent\((\d+)\)"(.*?)</li>', re.S)
COUNSEL_ROW_TITLE_PATTERN = re.compile(
    r'role="button"[^>]*>\s*(.*?)\s*<span class="state">(.*?)</span>', re.S)
COUNSEL_DETAIL_DATE_PATTERN = re.compile(r'<div class="question">.*?(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2})', re.S)
COUNSEL_DETAIL_TITLE_PATTERN = re.compile(r'<div class="question">.*?<p>(.*?)</p>', re.S)
COUNSEL_DETAIL_ITEM_PATTERN = re.compile(r'itemView\.ssg\?itemId=(\d+)')


def inquiry_message(recipient_name: str) -> str:
    return INQUIRY_MESSAGE.format(name=recipient_name.strip())


def _order_date_of(order_no: str) -> date:
    """orordNo의 앞 8자리가 주문일(YYYYMMDD)이다 - 20260905-7EF11A는 2026-09-05 주문."""
    try:
        return datetime.strptime(order_no[:8], "%Y%m%d").date()
    except ValueError:
        raise ParseError(f"주문번호에서 주문일을 읽을 수 없습니다 (orordNo={order_no}).") from None


def prepare_inquiries(context: BrowserContext, product_urls, headless: bool = False) -> None:
    """이번에 문의할 주문들의 취소/품절 여부를 주문목록으로 미리 읽어둔다.

    송장조회의 prepare_batch와 같은 목록을 쓴다 - 읽어둔 주문은 post_inquiry가
    상세 화면을 열지 않고 목록 구간으로 취소/품절만 본다. 못 읽으면 상세로 간다.
    """
    prepare_batch(context, [SimpleNamespace(product_url=u) for u in product_urls], headless=headless)


def _raise_if_cancelled_text(text: str, order_no: str) -> None:
    """취소/품절 표시가 있으면 문의를 남기지 않는다 (송장조회의 판정과 같은 표시)."""
    marker = next((m for m in CANCELLED_MARKERS if m in text), None)
    if marker:
        raise OrderCancelled(
            f"주문 화면에 '{marker}' 표시가 있어 문의를 남기지 않습니다 (orordNo={order_no}).")


def _abort_third_party(route) -> None:
    """문의 화면 전용 라우팅 - SSG 밖 호스트와 이미지·폰트는 끊고 나머지는 보낸다.

    페이지 라우팅이 걸리면 컨텍스트 공용 라우팅(이미지 차단)은 그 페이지에
    적용되지 않으므로 여기서 이미지도 같이 막는다.
    """
    request = route.request
    host = urlparse(request.url).netloc.lower()
    allowed = any(host == h or host.endswith("." + h) for h in INQUIRY_ALLOWED_HOSTS)
    if allowed and request.resource_type not in INQUIRY_BLOCKED_RESOURCE_TYPES:
        route.continue_()
    else:
        route.abort()


def _option_value(page, select: str, label: str) -> str | None:
    """select의 option 중 글자가 이 라벨인 것의 value (없으면 None)."""
    return page.evaluate(
        "([sel, label]) => { const o = Array.from(document.querySelectorAll(sel + ' option'))"
        ".find(o => o.textContent.trim() === label); return o ? o.value : null; }",
        [select, label])


def _choose_type(page, select: str, label: str, *, loads: str | None) -> None:
    """숨은 select에 값을 넣고 change(인라인 onchange)·sync(드롭다운 UI)를 올린다.

    loads: 이 선택이 불러오는 응답 URL의 일부 - 오기를 기다린다. 유형을 고르면
    내용 칸이 비어 있을 때만 안내 템플릿을 받아오므로(setCnslClsTempl) 그때는
    그 응답도 같이 기다린다 - 응답이 오는 대로 내용 칸을 덮어쓰기 때문이다.
    응답은 트리거 전부터 센다(놓치면 영영 못 받는다) - 동기 API는 호출 중에만
    이벤트를 넘겨주므로 센 것과 기다리는 것 사이에 응답이 새지 않는다.
    """
    value = _option_value(page, select, label)
    if not value:
        options = page.evaluate(
            "sel => Array.from(document.querySelectorAll(sel + ' option')).map(o => o.textContent.trim())",
            select)
        raise ParseError(f"문의유형 목록에 [{label}]이 없습니다 (있는 것: {options}).")
    wanted = [loads] if loads else []
    if page.locator(INQUIRY_CONTENT).input_value().strip() == "":
        wanted.append(INQUIRY_TEMPLATE_API)
    seen: list[str] = []
    page.on("response", lambda r: seen.append(r.url))
    page.evaluate(
        "([sel, value]) => { const $s = window.jQuery(sel); $s.val(value).trigger('change').trigger('sync'); }",
        [select, value])
    for mark in wanted:
        if any(mark in u for u in seen):
            continue
        try:
            page.wait_for_event("response", lambda r: mark in r.url, timeout=INQUIRY_STEP_WAIT_MS)
        except PlaywrightTimeoutError:
            raise ParseError(f"[{label}]을 골랐는데 {mark} 응답이 오지 않았습니다.") from None
    chosen = page.evaluate("sel => window.jQuery(sel).val()", select)
    if chosen != value:
        raise ParseError(f"문의유형 [{label}]이 잡히지 않았습니다 (select 값: {chosen}).")


def _open_item_layer(page, order_no: str) -> list[str]:
    """[주문상품 선택] 레이어를 열어 이 주문의 상품 줄(checkbox 값)들을 돌려준다.

    기본 1개월 목록에 없으면 주문일 하루짜리 조회로 다시 채운다.
    """
    keys_selector = f'{INQUIRY_ITEM_LAYER} input[name="ordItem"][value^="{order_no}_"]'
    table = page.locator(f"{INQUIRY_ITEM_LAYER} {INQUIRY_ITEM_TABLE}")
    with page.expect_response(lambda r: INQUIRY_ITEM_LIST_API in r.url, timeout=INQUIRY_STEP_WAIT_MS):
        page.locator(INQUIRY_ITEM_BUTTON).click()
    table.wait_for(state="attached", timeout=INQUIRY_STEP_WAIT_MS)
    keys = page.locator(keys_selector).evaluate_all("els => els.map(e => e.value)")
    day = f"{_order_date_of(order_no):%Y.%m.%d}"
    if not keys:
        with page.expect_response(lambda r: INQUIRY_ITEM_LIST_API in r.url, timeout=INQUIRY_STEP_WAIT_MS):
            page.evaluate("d => ordListForMyssgCs('0', d, d)", day)
        table.wait_for(state="attached", timeout=INQUIRY_STEP_WAIT_MS)
        keys = page.locator(keys_selector).evaluate_all("els => els.map(e => e.value)")
    if not keys:
        raise ParseError(f"[주문상품 선택] 목록에 이 주문이 없습니다 (orordNo={order_no}, 주문일 {day} 조회까지).")
    return keys


def _select_order_items(page, order_no: str) -> set[str]:
    """레이어에서 이 주문의 상품을 전부 체크하고 [확인]을 눌러 폼에 붙인다. 상품 itemId들을 돌려준다."""
    keys = _open_item_layer(page, order_no)
    item_ids: set[str] = set()
    for key in keys:
        item_id = page.locator(f"#itemId{key}").get_attribute("value") or ""
        if item_id:
            item_ids.add(item_id)
        box = page.locator(f"#ordItem_{key}")
        if not box.is_checked():
            page.locator(f'label[for="ordItem_{key}"]').click()
        if not box.is_checked():
            raise ParseError(f"주문상품 {key}이(가) 체크되지 않았습니다.")
    if not item_ids:
        raise ParseError(f"[주문상품 선택] 목록에서 이 주문의 상품번호(itemId)를 읽지 못했습니다 (orordNo={order_no}).")
    page.locator(INQUIRY_ITEM_CONFIRM).click()
    selected = page.locator(f"{INQUIRY_ITEM_SELECTED} .product_info")
    try:
        selected.first.wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError("[주문상품 선택]에서 [확인]을 눌렀는데 폼에 상품이 붙지 않았습니다.") from None
    attached = page.locator('input[name="cnslItemBaseDto.ordNo"]').evaluate_all("els => els.map(e => e.value)")
    if order_no not in attached:
        raise ParseError(f"폼에 붙은 주문번호가 이 주문이 아닙니다 (orordNo={order_no}, 폼: {attached}).")
    return item_ids


def _prepare_inquiry_form(page, order_no: str, message: str) -> set[str]:
    """E-mail 상담 화면을 열어 유형·상품·제목·내용을 채운다 - [등록]은 누르지 않는다.

    post_inquiry가 부르고, 등록 없이 채우기까지만 재보는 검증 스크립트도 이것을 쓴다.
    돌려주는 것은 이 주문의 상품 itemId들(문의내역 확인에 쓴다).
    """
    _goto_logged_in(page, COUNSEL_FORM_URL)
    page.wait_for_load_state("load")
    try:
        page.locator(INQUIRY_SUBMIT).wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError(f"E-mail 상담 화면이 뜨지 않았습니다 (url={page.url}).") from None
    _choose_type(page, INQUIRY_TYPE_SELECT, INQUIRY_TYPE, loads=INQUIRY_SUBTYPE_API)
    _choose_type(page, INQUIRY_SUBTYPE_SELECT, INQUIRY_SUBTYPE, loads=None)
    item_ids = _select_order_items(page, order_no)
    page.locator(INQUIRY_TITLE).fill(message[:INQUIRY_TITLE_MAX])
    page.locator(INQUIRY_CONTENT).fill(message)
    if page.locator(INQUIRY_TITLE).input_value().strip() != message[:INQUIRY_TITLE_MAX]:
        raise ParseError("제목이 입력되지 않았습니다.")
    if page.locator(INQUIRY_CONTENT).input_value().strip() != message:
        raise ParseError("문의내용이 입력되지 않았습니다.")
    types = page.evaluate(
        "([a, b]) => [a, b].map(sel => window.jQuery(sel + ' option:selected').text().trim())",
        [INQUIRY_TYPE_SELECT, INQUIRY_SUBTYPE_SELECT])
    if types != [INQUIRY_TYPE, INQUIRY_SUBTYPE]:
        raise ParseError(f"문의유형이 {INQUIRY_TYPE}/{INQUIRY_SUBTYPE}으로 잡히지 않았습니다 (화면: {types}).")
    return item_ids


def _get_html(context: BrowserContext, url: str) -> str | None:
    """브라우저 쿠키로 GET - 로그인 화면으로 넘어가거나 실패하면 None."""
    try:
        response = context.request.get(url)
    except Exception:  # noqa: BLE001 - 통신 실패는 '못 읽음'
        return None
    if not response.ok or "member.ssg.com" in response.url:
        return None
    return response.text()


def _parse_counsel_list(page_html: str) -> list[dict]:
    """E-mail 답변확인 목록 HTML에서 줄마다 문의번호·제목·상태 (최신순)."""
    items: list[dict] = []
    for m in COUNSEL_ROW_PATTERN.finditer(page_html):
        t = COUNSEL_ROW_TITLE_PATTERN.search(m.group(2))
        if not t:
            continue
        items.append({
            "cnsl_id": m.group(1),
            "title": html_mod.unescape(re.sub(r"\s+", " ", t.group(1))).strip(),
            "state": html_mod.unescape(t.group(2)).strip(),
        })
    return items


def _fetch_counsel_list(context: BrowserContext, page_no: int) -> list[dict] | None:
    page_html = _get_html(context, COUNSEL_LIST_URL.format(page=page_no))
    if page_html is None or 'id="listCounsel"' not in page_html:
        return None
    return _parse_counsel_list(page_html)


def _fetch_counsel_detail(context: BrowserContext, cnsl_id: str) -> dict | None:
    """문의 상세 - 작성일(datetime)·제목·상품 itemId 집합."""
    detail_html = _get_html(context, COUNSEL_DETAIL_URL.format(cnsl_id=cnsl_id))
    if detail_html is None:
        return None
    d = COUNSEL_DETAIL_DATE_PATTERN.search(detail_html)
    t = COUNSEL_DETAIL_TITLE_PATTERN.search(detail_html)
    if not d or not t:
        return None
    return {
        "cnsl_id": cnsl_id,
        "written_at": datetime.strptime(d.group(1), "%Y.%m.%d %H:%M"),
        "title": html_mod.unescape(re.sub(r"\s+", " ", t.group(1))).strip(),
        "item_ids": set(COUNSEL_DETAIL_ITEM_PATTERN.findall(detail_html)),
    }


def _describe_listed(entry: dict) -> str:
    return (f"{entry.get('state') or '상태 모름'} {entry['written_at']:%Y.%m.%d %H:%M} "
            f"(문의번호 {entry['cnsl_id']})")


def _find_listed_inquiry(context: BrowserContext, message: str, item_ids: set[str],
                         since: date, *, max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> dict | None:
    """문의내역에서 since 이후에 쓴, 같은 제목이고 이 주문 상품의 문의를 찾는다.

    목록은 최신순이고 날짜가 없어 같은 제목의 줄만 상세를 열어 본다. 같은 제목의
    상세가 since보다 오래됐으면 그 아래는 전부 더 오래된 것이라 거기서 멈추고,
    한 페이지에 같은 제목이 없으면 그 페이지 마지막 줄의 상세로 날짜를 재서
    다음 페이지로 갈지 정한다. 목록을 못 읽으면 ParseError - 모르는 채로
    등록하지 않는다.
    """
    for page_no in range(1, max_pages + 1):
        rows = _fetch_counsel_list(context, page_no)
        if rows is None:
            raise ParseError("E-mail 답변확인 목록을 읽지 못했습니다 (로그인 세션이 없거나 화면이 바뀜).")
        if not rows:
            return None
        checked_last = False
        for i, row in enumerate(rows):
            if row["title"] != message:
                continue
            detail = _fetch_counsel_detail(context, row["cnsl_id"])
            if detail is None:
                raise ParseError(f"문의 상세를 읽지 못했습니다 (문의번호 {row['cnsl_id']}).")
            if i == len(rows) - 1:
                checked_last = True
            if detail["written_at"].date() < since:
                return None
            if detail["item_ids"] & item_ids:
                return {**row, **detail}
        if not checked_last:
            tail = _fetch_counsel_detail(context, rows[-1]["cnsl_id"])
            if tail is not None and tail["written_at"].date() < since:
                return None
    return None


def _confirm_inquiry_listed(context: BrowserContext, item_ids: set[str], message: str,
                            not_before: date | None = None) -> str:
    """문의내역 첫 페이지에 이 주문 상품의 같은 문구 문의가 오늘(not_before 이후) 올라갔는지 확인한다."""
    since = not_before or date.today()
    for attempt in range(1, INQUIRY_HISTORY_TRIES + 1):
        found = _find_listed_inquiry(context, message, item_ids, since, max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < INQUIRY_HISTORY_TRIES:
            common.sleep(INQUIRY_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"완료 문구는 받았지만 E-mail 답변확인에서 확인되지 않았습니다. "
        f"다시 남기기 전에 SSG E-mail 답변확인 화면에서 '{message}'가 있는지 직접 확인해주세요.")


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """E-mail 상담(배송 > 배송 일정 확인)을 남기고 완료 문구를 돌려준다.

    취소/품절 주문은 남기지 않고, 문의내역에 이 주문의 같은 문의가 주문일
    이후에 이미 있으면 AlreadyInquired로 넘긴다. 완료 문구를 받은 뒤 E-mail
    답변확인에 오늘 자로 올라갔는지까지 확인한다(_confirm_inquiry_listed).
    어디서든 어긋나면 ParseError/BlockedError - 남겼는지 불확실한 채로 성공이라
    하지 않는다.
    """
    order_no = extract_order_no(product_url)
    message = inquiry_message(recipient_name)
    order_date = _order_date_of(order_no)
    page = context.new_page()
    page.route("**/*", _abort_third_party)
    try:
        section = _listed_orders.get(id(context), {}).get(order_no)
        if section is not None:
            _raise_if_cancelled_text(section, order_no)
        else:
            _goto_logged_in(page, product_url)
            if not common.wait_for_text(page, order_no[:8]):
                raise ParseError(f"주문상세가 그려지지 않았습니다 (orordNo={order_no}).")
            _raise_if_cancelled_text(page.inner_text("body"), order_no)

        item_ids = _prepare_inquiry_form(page, order_no, message)
        existing = _find_listed_inquiry(context, message, item_ids, order_date)
        if existing is not None:
            raise AlreadyInquired(f"E-mail 답변확인에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

        dialogs: list[tuple[str, str]] = []

        def _on_dialog(dialog) -> None:
            dialogs.append((dialog.type, dialog.message))
            # 답변 수신 확인(confirm)은 승인, 완료 안내(alert)는 닫는다 - 둘 다 accept.
            dialog.accept()

        page.on("dialog", _on_dialog)
        page.locator(INQUIRY_SUBMIT).click()
        with contextlib.suppress(PlaywrightTimeoutError):
            page.wait_for_url(lambda url: INQUIRY_DONE_URL in url, wait_until="commit",
                              timeout=INQUIRY_STEP_WAIT_MS)
        done = [m for t, m in dialogs if INQUIRY_DONE_TEXT in m]
        if not done:
            seen = " / ".join(f"{t}: {m}" for t, m in dialogs) or "(뜬 창 없음)"
            if INQUIRY_DONE_URL in page.url:
                raise ParseError(f"E-mail 답변확인 화면으로 넘어갔지만 완료 문구를 받지 못했습니다 ({seen}).")
            raise ParseError(f"[등록]을 눌렀는데 완료 문구가 오지 않았습니다 ({seen}).")
        listed = _confirm_inquiry_listed(context, item_ids, message)
        return f"{done[0].strip().split('.')[0]} · E-mail 답변확인: {listed}"
    finally:
        page.close()
