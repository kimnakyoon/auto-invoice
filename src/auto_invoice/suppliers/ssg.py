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

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    AdapterError,
    BlockedError,
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
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            common.safe_print("[ssg] 로그인 세션이 없어 자동 로그인을 시도합니다.")
            if not _auto_login(page):
                raise BlockedError("SSG 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
            if _looks_like_login_page(page):
                raise BlockedError("SSG 로그인 후에도 여전히 로그인 페이지입니다.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("SSG 로그인 후에도 여전히 로그인 페이지입니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        page.close()
