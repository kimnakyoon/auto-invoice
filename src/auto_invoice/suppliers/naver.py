"""네이버페이(Naver Pay) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://orders.pay.naver.com/order/status/<주문번호>
  (구매내역 목록의 "주문 상세 보기" 링크와 동일. 목록 페이지
  https://pay.naver.com/pc/history 자체는 주문 식별 정보가 없어 조회에
  쓸 수 없다.)
- 네이버는 롯데온/지마켓과 달리 아이디+비밀번호를 스크립트로 채워 넣으면
  "보안을 위해 추가 확인을 해주세요"라는 캡차(가상 영수증에서 가장 비싼
  품목 찾기)를 띄워 로그인 자체를 막는다. 그래서 SSG처럼 완전 자동
  로그인은 불가능하고, 롯데온/지마켓과 동일하게 아이디만 자동 입력한 뒤
  사람이 직접 비밀번호+보안 확인을 완료하게 한다. 대신 로그인 상태
  유지 쿠키가 오래가서, 최초 1회만 사람이 로그인하면 이후에는
  storage_state 재사용만으로 계속 자동 로그인된다.
- 사용자가 네이버 계정을 2개(각각 다른 주문을 구매) 쓰고 있어, 어느
  계정에 특정 주문이 있는지 미리 알 수 없다. 로그인되지 않은 계정으로
  다른 계정의 주문상세 URL을 열면 에러 없이 그냥 본인 구매내역 목록
  (https://pay.naver.com/pc/history)으로 조용히 리다이렉트된다 - 이걸로
  "이 계정 소유가 아님"을 판별한다. 그래서 첫 번째 계정에서 못 찾으면
  두 번째 계정으로, 두 번째에서도 못 찾으면 다시 첫 번째로(=순서 상관없이
  결국 둘 다 확인) 넘어가도록 만들었다. 두 계정은 완전히 별도의
  BrowserContext(별도 storage_state 파일: auth/naver_state.json,
  auth/naver2_state.json)로 관리한다 - 오케스트레이터는 SITE_KEY
  "naver" 하나만 알고 첫 번째 계정용 context만 만들어주므로, 두 번째
  계정용 context는 이 모듈이 내부적으로 만들고 로그인 직후 직접
  storage_state를 저장한다.
- 주문상세 페이지에서 "배송조회" 버튼을 누르면(모달/새 탭이 아니라)
  같은 탭이 https://orders.pay.naver.com/order/delivery/tracking/... 로
  이동하며 그 화면에 "<택배사명>\n송장번호\n<번호>" 형태로 바로 나온다.
  버튼 자체가 없으면(상품준비중 등 발송 전) 아직 미발급인 것이다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from .base import BlockedError, OrderNotFound, ParseError, TrackingNotAvailableYet

load_dotenv()

LOGIN_ID_SELECTOR = "#id"

DOMAINS = {"orders.pay.naver.com"}
SITE_KEY = "naver"

SECOND_ACCOUNT_STATE_KEY = "naver2"

ORDER_STATUS_URL = (
    "https://orders.pay.naver.com/order/status/{order_no}"
    "?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"
)

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

TRACK_BUTTON_TEXT = "배송조회"

COURIER_TRACKING_PATTERN = re.compile(r"([가-힣A-Za-z0-9()]{2,20})\n송장번호\n([0-9][0-9\-]{5,})")
NOT_YET_PATTERNS = ["상품준비중", "결제완료", "배송준비중", "발송준비", "취소"]

# CJ대한통운/롯데택배는 화면 표기가 정식 명칭과 달라서(예: "대한통운") 업로드
# 파일에는 정식 명칭으로 맞춰 넣는다 (지마켓/SSG 어댑터와 동일한 규칙).
COURIER_NORMALIZATION = [
    ("대한통운", "CJ대한통운"),
    ("CJ", "CJ대한통운"),
    ("롯데", "롯데택배"),
]

# 두 번째 계정용 context 캐시. GUI는 같은 파이썬 프로세스 안에서 실행 버튼을
# 여러 번 누를 수 있고 그때마다 orchestrator가 새 Browser를 여니, 브라우저
# 인스턴스별로 캐시해야 지난 실행에서 이미 닫힌 context를 재사용하지 않는다.
_second_context_cache: dict[int, BrowserContext] = {}


def _normalize_courier(raw: str) -> str:
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword in raw:
            return canonical
    return raw


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _looks_like_login_page(page) -> bool:
    """비밀번호 입력창은 네이버가 보안상 타이핑 중 수시로 다시 그려서
    count()가 순간적으로 0이 되는 경우가 있어 신뢰할 수 없다. 로그인
    성공 시 반드시 nid.naver.com을 벗어나므로 URL만으로 판단한다."""
    page.wait_for_timeout(1200)
    return "nid.naver.com" in page.url


def _redirected_away(page) -> bool:
    """로그인은 되어 있지만 그 계정 소유의 주문이 아니면 에러 없이
    구매내역 목록으로 조용히 리다이렉트된다."""
    return "orders.pay.naver.com" not in page.url


def _prefill_login_id(page, naver_id: str | None) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    if not naver_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(naver_id)
    except Exception:
        pass


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


def _wait_for_manual_login(page) -> bool:
    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1200  # _looks_like_login_page 내부에서 1200ms 대기함
    return False


def _get_second_context(primary_context: BrowserContext, headless: bool) -> BrowserContext:
    browser = primary_context.browser
    if browser is None:  # pragma: no cover - 방어적 코드, 실제로는 항상 browser가 있음
        raise BlockedError("두 번째 네이버 계정용 브라우저를 준비하지 못했습니다.")

    cache_key = id(browser)
    cached = _second_context_cache.get(cache_key)
    if cached is not None:
        return cached

    state_path = browser_mod.state_path(SECOND_ACCOUNT_STATE_KEY)
    if state_path.exists():
        context = browser.new_context(storage_state=str(state_path))
    else:
        context = browser.new_context()
    _second_context_cache[cache_key] = context
    return context


def _ensure_logged_in(page, headless: bool, naver_id_env: str, account_label: str) -> bool:
    """수동 로그인을 실제로 수행했으면 True를 반환한다 (호출자가 이때만
    storage_state를 저장하면 된다)."""
    if not _looks_like_login_page(page):
        return False
    if headless:
        raise BlockedError(
            f"네이버페이 로그인이 필요합니다({account_label}). 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
        )
    _prefill_login_id(page, os.environ.get(naver_id_env))
    _safe_print(f"[naver] ({account_label}) 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호 입력과 보안 확인을 완료해주세요.")
    _safe_print(f"[naver] ({account_label}) 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
    if not _wait_for_manual_login(page):
        raise BlockedError(f"네이버페이({account_label}) 로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
    return True


def _scrape_tracking_from_page(page, order_no: str) -> TrackingResult:
    button = page.get_by_text(TRACK_BUTTON_TEXT, exact=True)
    if button.count() == 0:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise ParseError(f"배송조회 버튼을 찾지 못했습니다 (주문번호={order_no}).")

    try:
        button.first.click(timeout=3000)
    except Exception as e:
        raise ParseError(f"배송조회 버튼 클릭에 실패했습니다 (주문번호={order_no}): {e}") from e
    page.wait_for_timeout(2000)

    body_text = page.inner_text("body")
    match = COURIER_TRACKING_PATTERN.search(body_text)
    if not match:
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise ParseError(f"화면에서 송장번호 텍스트를 찾지 못했습니다 (주문번호={order_no}).")

    courier = _normalize_courier(match.group(1).strip())
    tracking_no = re.sub(r"[^0-9]", "", match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def _get_tracking_from_account(
    context: BrowserContext, order_no: str, headless: bool, naver_id_env: str, account_label: str
) -> TrackingResult:
    page = context.new_page()
    try:
        url = ORDER_STATUS_URL.format(order_no=order_no)
        page.goto(url, wait_until="domcontentloaded")

        did_login = _ensure_logged_in(page, headless, naver_id_env, account_label)
        if did_login:
            # 네이버는 로그인 성공 시 원래 요청했던 URL로 자동 복귀하지만,
            # 혹시 그대로 로그인 페이지에 남아있으면 한 번 더 이동을 시도한다.
            if _looks_like_login_page(page):
                page.goto(url, wait_until="domcontentloaded")
                if _looks_like_login_page(page):
                    raise BlockedError(f"네이버페이({account_label}) 로그인 후에도 여전히 로그인 페이지입니다.")
            state_path = browser_mod.state_path(SITE_KEY if account_label == "1" else SECOND_ACCOUNT_STATE_KEY)
            context.storage_state(path=str(state_path))

        if _redirected_away(page):
            raise OrderNotFound(f"이 계정({account_label})에서 주문을 찾을 수 없습니다 (주문번호={order_no}).")

        return _scrape_tracking_from_page(page, order_no)
    finally:
        page.close()


def get_tracking(context: BrowserContext, product_url: str, headless: bool = True) -> TrackingResult:
    order_no = extract_order_no(product_url)

    try:
        return _get_tracking_from_account(context, order_no, headless, "NAVER_ID", "1")
    except OrderNotFound:
        pass

    second_context = _get_second_context(context, headless)
    return _get_tracking_from_account(second_context, order_no, headless, "NAVER_ID2", "2")
