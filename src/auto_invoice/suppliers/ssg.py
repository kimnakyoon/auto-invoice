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
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
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

# CJ대한통운은 화면에 "대한통운"으로 짧게 나오는 경우가 많아, 정식 명칭으로
# 맞춰 넣는다 (지마켓 어댑터와 동일한 정규화 규칙).
COURIER_NORMALIZATION = [
    ("대한통운", "CJ대한통운"),
    ("CJ", "CJ대한통운"),
    ("롯데", "롯데택배"),
    ("DELIBOX", "딜리박스"),
]


def _normalize_courier(raw: str) -> str:
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword in raw:
            return canonical
    return raw


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("orordNo")
    if not values:
        raise ParseError(f"URL에서 orordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    if "member.ssg.com" not in page.url and "login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


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
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함
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
    body_text = page.inner_text("body")

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

    courier = _normalize_courier(tracking_match.group(1).strip())
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            _safe_print("[ssg] 로그인 세션이 없어 자동 로그인을 시도합니다.")
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
