"""지마켓(Gmarket) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://my.gmarket.co.kr/ko/pc/detail/basic/<장바구니번호>
  (화면에 보이는 "주문번호"와는 다른, URL 전용 내부 번호다. 파싱할 필요 없이
  URL 그대로 열면 된다.)
- "배송조회" 버튼을 누르면 뜨는 모달은 tracking.gmarket.co.kr 도메인의
  iframe이라서 메인 페이지 DOM에서는 텍스트를 못 읽는다 - 반드시 그 프레임
  안에서 읽어야 한다. 모달 안에는 "택배사명 송장번호"가 같은 줄에 붙어서
  나온다 (예: "CJ택배 501707425705").
- 지마켓은 롯데온(Imperva)과 별개로 Cloudflare Turnstile 봇 확인 화면이 뜬다.
  이 화면은 사람이 직접 체크박스를 눌러도 "확인 중..."에서 넘어가지 않는
  경우가 있었다 (자동화 브라우저 자체를 의심하는 것으로 보임). 우회를
  시도하지 않고, 롯데온의 Imperva 차단과 동일하게 BlockedError로 알리고
  사람이 직접 해결하도록 한다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import BlockedError, ParseError, TrackingNotAvailableYet, normalize_option

load_dotenv()

LOGIN_ID_SELECTOR = "#typeMemberInputId"

DOMAINS = {"gmarket.co.kr", "www.gmarket.co.kr", "my.gmarket.co.kr"}
SITE_KEY = "gmarket"

DEFAULT_COURIER = "택배"  # 모달에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분
BOT_CHECK_WAIT_TIMEOUT_MS = 3 * 60 * 1000  # 봇 확인 통과 대기 최대 3분

TRACKING_BUTTON_TEXTS = ["배송조회"]

# 모달(iframe) 안, 주소/배송요청사항 뒤에 "택배사명 송장번호"가 붙어서 나온다.
TRACKING_LINE_PATTERN = re.compile(r"([가-힣A-Za-z]{2,20})\s*([0-9][0-9\-]{7,})\s*$")
NOT_YET_PATTERNS = ["배송준비중", "상품준비중", "결제확인중", "주문확인중"]
BOT_CHECK_PATTERNS = ["사람인지 확인", "봇(Bot)이란"]

# 지마켓 화면에 뜨는 택배사 표기가 샵마인이 기대하는 정식 명칭과 달라서
# (예: "CJ택배") 업로드 파일에는 정식 명칭으로 맞춰 넣는다. 위에서부터
# 순서대로 검사하므로 더 구체적인 키워드를 먼저 둔다.
COURIER_NORMALIZATION = [
    ("대한통운", "CJ대한통운"),
    ("CJ", "CJ대한통운"),
    ("롯데", "롯데택배"),
]


def _normalize_courier(raw: str) -> str:
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword in raw:
            return canonical
    return raw


def extract_order_id(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        raise ParseError(f"URL에서 주문 식별 번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _looks_like_login_page(page) -> bool:
    page.wait_for_timeout(1500)
    if "signinssl.gmarket.co.kr" not in page.url and "/login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


def _looks_like_bot_check(page) -> bool:
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    return any(p in body_text for p in BOT_CHECK_PATTERNS)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    gmarket_id = os.environ.get("GMARKET_ID")
    if not gmarket_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(gmarket_id)
    except Exception:
        pass


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


def _wait_for_manual_login(page) -> bool:
    """비밀번호 입력창이 사라질 때까지(=로그인 완료) 화면 상태를 폴링하며 대기한다."""
    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함
    return False


def _wait_for_bot_check_to_clear(page) -> bool:
    elapsed_ms = 0
    while elapsed_ms < BOT_CHECK_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(3000)
        elapsed_ms += 3000
        if not _looks_like_bot_check(page):
            return True
    return False


def _click_tracking_button(page) -> bool:
    for text in TRACKING_BUTTON_TEXTS:
        loc = page.get_by_text(text, exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=3000)
            page.wait_for_timeout(1500)  # 모달/iframe 렌더링 대기
            return True
        except Exception:
            continue
    return False


def _read_tracking_frame_text(page) -> str:
    """배송조회 모달은 tracking.gmarket.co.kr iframe이라 메인 페이지 DOM에는
    텍스트가 없다 - 그 프레임 안에서 읽어야 한다."""
    for frame in page.frames:
        if "tracking.gmarket.co.kr" in frame.url:
            try:
                return frame.inner_text("body")
            except Exception:
                continue
    return ""


def _select_by_order_option(lines: list[str], matched_line_indices: list[int], order_option: str | None):
    """샵마인 엑셀의 "주문옵션" 값이 어느 송장번호 줄 근처(보통 상품명/옵션은
    송장번호 줄보다 앞에 나온다)에만 유일하게 나타나면 그 인덱스를 쓴다.
    0개(표기가 안 맞음) 또는 2개 이상(애매함) 매칭되면 None - 호출자가
    기존 방식(사람 확인 요청)으로 넘어간다."""
    if len(matched_line_indices) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    candidates = []
    prev_idx = -1
    for idx in matched_line_indices:
        # window 시작을 이전 매치 줄 이후로 묶어서, 앞 상품의 옵션 텍스트가
        # 다음 상품 판단에 섞여 들어가지(bleed) 않게 한다.
        window = "\n".join(lines[max(prev_idx + 1, idx - 15) : idx])
        if target in normalize_option(window):
            candidates.append(idx)
        prev_idx = idx
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page, order_id: str, order_option: str | None = None) -> TrackingResult:
    clicked = _click_tracking_button(page)
    if not clicked:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orderId={order_id}).")
        raise ParseError(f"배송조회 버튼을 찾지 못했습니다 (orderId={order_id}).")

    frame_text = _read_tracking_frame_text(page)
    if not frame_text:
        raise ParseError(f"배송조회 모달(iframe)에서 내용을 읽지 못했습니다 (orderId={order_id}).")

    lines = frame_text.splitlines()
    tracking_matches: dict[int, re.Match] = {}
    for idx, line in enumerate(lines):
        m = TRACKING_LINE_PATTERN.search(line.strip())
        if m:
            tracking_matches[idx] = m

    if not tracking_matches:
        if any(p in frame_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (orderId={order_id}).")
        raise ParseError(f"모달에서 송장번호 텍스트를 찾지 못했습니다 (orderId={order_id}).")

    distinct_tracking_nos = {re.sub(r"[^0-9]", "", m.group(2)) for m in tracking_matches.values()}
    matched_idx = _select_by_order_option(lines, list(tracking_matches.keys()), order_option)
    if matched_idx is None:
        if len(distinct_tracking_nos) > 1:
            # 한 주문이 상품별로 나눠 배송되어 서로 다른 송장번호가 여러 개
            # 보이는 경우다 - 어느 걸 써야 하는지 확신할 수 없어 사람이
            # 확인하게 한다 (무신사 어댑터와 동일한 안전 규칙).
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (orderId={order_id}) - 상품별로 나눠 배송된 것으로 보입니다.")
        matched_idx = max(tracking_matches)  # 기존 동작(마지막 매치) 유지

    tracking_match = tracking_matches[matched_idx]
    courier = _normalize_courier(tracking_match.group(1).strip() or DEFAULT_COURIER)
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_id = extract_order_id(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_bot_check(page):
            if headless:
                raise BlockedError(
                    "지마켓 봇 확인(Cloudflare) 화면이 떴습니다. 먼저 --headless 없이 실행해 수동으로 통과해주세요."
                )
            _safe_print("[gmarket] 봇 확인 화면이 떴습니다. 뜬 브라우저 창에서 직접 체크박스를 눌러 통과해주세요.")
            if not _wait_for_bot_check_to_clear(page):
                raise BlockedError("봇 확인 대기 시간(3분)이 지났습니다. 통과 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if headless:
                raise BlockedError(
                    "지마켓 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[gmarket] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[gmarket] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        return _scrape_tracking_from_page(page, order_id, order_option)
    finally:
        page.close()
