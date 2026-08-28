"""CJ온스타일(base.cjonstyle.com) 공급사 어댑터.

※ 현재 이 어댑터는 registry.py에 등록되어 있지 않아 실제로는 쓰이지 않는다.
   아래에 적어둔 대로 로그인 폼의 Cloudflare Turnstile("사람인지 확인")이
   Playwright로 띄운 브라우저 자체를 감지해서, 사람이 직접 체크박스를 눌러도
   "확인 실패"가 뜨며 로그인이 되지 않는다(실제 로그인된 크롬 세션의 쿠키를
   storage_state로 옮겨와도 세션이 유지되지 않았다). 그래서 이 사이트는
   cjonstyle_bridge.py가 claude-in-chrome 확장으로 사용자의 실제 크롬
   브라우저를 조작해서 조회한다. 이 파일은 나중에 Playwright로도 로그인이
   가능해질 때를 대비해, 로그인 이후의 화면 구조/파싱 로직을 검증된 상태로
   남겨둔 것이다(로그인 단계를 제외한 나머지는 실제 화면으로 확인했다).

리버스엔지니어링 결과:
- 주문상세 URL: https://base.cjonstyle.com/p/myzone/orderInfo/<주문번호>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고, 경로의
  마지막 세그먼트를 주문번호로 쓴다.)
- 로그인이 안 되어 있을 때, 주문상세(orderInfo) URL로 직접 들어가면 다른
  사이트들과 달리 로그인 페이지로 리다이렉트되지 않고 조용히 홈
  (display.cjonstyle.com)으로 리다이렉트된다 - 그래서 주문상세 URL 이동
  결과만으로는 로그인 필요 여부를 판단할 수 없고, 이동 후 URL이 여전히
  "/p/myzone/" 경로인지로 판단한다(_looks_authenticated). 로그인이 필요하면
  로그인 페이지로 확실히 리다이렉트되는 주문목록(orderList) URL을 거쳐서
  로그인을 진행한다. 그 URL은 https://base.cjonstyle.com/p/page/account/login?...
  으로 리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#id_input, 비밀번호
  input#password_input, 로그인 버튼은 role=button 이름 "로그인"(exact).
  사용자가 완전 자동 로그인을 요청했으나, 실제로 확인해보니 로그인 폼에
  Cloudflare Turnstile "사람인지 확인" 체크박스가 있고 이걸 통과하지 않으면
  로그인 버튼을 눌러도 그대로 로그인 페이지에 머문다 (Hmall의 reCAPTCHA와
  동일한 종류의 봇 차단 - 자동으로 우회하지 않는다). 그래서 Hmall 어댑터와
  동일한 방식으로 아이디만 자동 입력하고, 비밀번호 입력·"사람인지 확인" 체크·
  로그인 버튼 클릭은 사람이 직접 하도록 만들었다. 로그인 세션은
  storage_state(쿠키)로 저장되므로, 최초 1회만 사람이 직접 로그인하면 이후
  실행부터는 쿠키로 재로그인 없이 바로 조회된다 (사용자가 원한 "쿠키로 자동
  로그인"은 이 방식으로 충족된다).
- 주문상세 페이지의 "배송조회" 버튼을 클릭하면 새 탭이나 모달이 아니라 같은
  탭에서 https://base.cjonstyle.com/p/myzone/deliveryTracking/sheet?orderNo=
  <주문번호>&orderGSeq=<그룹순번>&orderWSeq=<배송순번> 으로 이동한다 (Hmall과
  동일한 패턴). 이 페이지는 API 호출 없이 서버에서 렌더링된 화면 텍스트에
  "송장번호" 다음 줄에 송장번호, "택배업체" 다음 줄에 "<택배사명> <전화번호>"
  형식으로 값이 그대로 노출되므로 화면 텍스트를 정규식으로 읽는다. 상단에
  "배송 상세정보가 존재하지않습니다 / 송장이 미등록되었거나..." 라는 문구가
  실제 송장번호가 있는 주문에도 뜨는 것을 확인했다 (택배사 실시간 배송이력
  조회가 안 될 때 뜨는 문구로 보이며, 송장번호/택배업체 필드 존재 여부와는
  무관하다) - 그래서 이 문구는 무시하고 "송장번호" 필드 존재 여부로만
  판단한다. 실제 확인한 값: 주문번호 20260826017435, 송장번호
  316726014614, 택배업체 "롯데택배 1588-2121" (정식 명칭 그대로 노출됨).
- 상품이 여러 개라 "배송조회" 버튼이 여러 개 뜨는 경우, 샵마인 엑셀의
  "주문옵션" 값으로 어느 버튼인지 특정할 수 있으면 그 버튼만 클릭한다.
  특정할 수 없으면(다른 어댑터와 동일한 안전 규칙) 전부 클릭해서 실제로 서로
  다른 송장인지 비교하고, 다르면 사람이 확인하도록 예외를 던진다. 클릭할
  때마다 같은 탭이 이동해버리므로, 다음 버튼을 클릭하기 전에 주문상세
  페이지로 다시 돌아간다(Hmall과 동일). 환불된 상품은 "배송조회"가 아니라
  "회수조회" 버튼이 뜨는 것을 확인했다 - exact text match라 자동으로
  제외된다.
- 아직 발송 전 상태 문구(NOT_YET_PATTERNS): 실제 미발송 주문 화면에서
  "상품준비중"을 확인했다 (이 상태에서는 "배송조회" 버튼 자체가 없다). 배송조회
  버튼을 눌렀는데도 결과 페이지에 "송장번호" 필드가 없는 경우도 같은 사유로
  간주해 TrackingNotAvailableYet으로 처리한다. 나머지 값들("결제완료",
  "배송준비중", "주문접수")은 다른 어댑터에서 흔히 보이는 값으로 추정해둔 것이라
  다르게 나오면 조정이 필요하다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from ..models import TrackingResult
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#id_input"

DOMAINS = {"base.cjonstyle.com"}
SITE_KEY = "cjonstyle"

LOGIN_PATH = "/p/page/account/login"
MYZONE_PATH_PREFIX = "/p/myzone/"
# 비로그인 상태로 orderInfo(주문상세) URL에 직접 접근하면, 다른 사이트들처럼
# 로그인 페이지로 리다이렉트되는 게 아니라 조용히 홈(display.cjonstyle.com)으로
# 리다이렉트되는 것을 확인했다. 그래서 주문상세 URL만 봐서는 로그인 필요
# 여부를 판단할 수 없고, 로그인 페이지로 확실히 리다이렉트되는 이 URL을
# 거쳐서 로그인 여부를 확인/처리한다.
LOGIN_CHECK_URL = "https://base.cjonstyle.com/p/myzone/orderList?listType=ORDER&orderStatus=ALL"
DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분 (사람이 캡차+비번 입력)
TRACKING_NAV_WAIT_TIMEOUT_MS = 5 * 1000  # 배송조회 클릭 후 페이지 이동 대기 최대 5초

TRACKING_LINK_TEXT = "배송조회"
TRACKING_URL_MARKER = "deliveryTracking/sheet"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문접수"]

TRACKING_NO_PATTERN = re.compile(r"송장번호\s*\n\s*([0-9]+)")
COURIER_PATTERN = re.compile(r"택배업체\s*\n\s*(\S+)")

# CJ대한통운/롯데택배/딜리박스가 축약형/코드로 나올 수 있어 업로드 파일에는
# 정식 명칭으로 맞춰 넣는다 (다른 어댑터와 동일한 규칙).
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
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _looks_like_login_page(page: Page) -> bool:
    page.wait_for_timeout(1500)
    if LOGIN_PATH not in urlparse(page.url).path:
        return False
    return page.locator("input[type='password']").count() > 0


def _looks_authenticated(page: Page) -> bool:
    """주문상세 URL로 이동한 뒤에도 그 경로(마이존)에 그대로 있는지 확인한다.

    비로그인 상태면 로그인 폼이 아니라 홈으로 조용히 리다이렉트되므로,
    로그인 폼 유무가 아니라 마이존 경로에 남아있는지로 판단해야 한다."""
    return MYZONE_PATH_PREFIX in urlparse(page.url).path


def _prefill_login_id(page: Page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다.

    로그인 버튼 자동 클릭도 하지 않는다 - Cloudflare Turnstile "사람인지 확인"
    체크박스를 통과하지 않으면 로그인이 진행되지 않는 것을 확인했다(위
    docstring 참고).
    """
    cjonstyle_id = os.environ.get("CJONSTYLE_ID")
    if not cjonstyle_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(cjonstyle_id)
    except Exception:
        pass


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


def _wait_for_manual_login(page: Page) -> bool:
    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함
    return False


def _parse_tracking_page(body_text: str, order_no: str) -> tuple[str, str]:
    tracking_match = TRACKING_NO_PATTERN.search(body_text)
    if not tracking_match:
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    courier_match = COURIER_PATTERN.search(body_text)
    courier_raw = courier_match.group(1).strip() if courier_match else None
    courier = _normalize_courier(courier_raw) if courier_raw else DEFAULT_COURIER

    return tracking_no, courier


def _click_tracking_link(page: Page, product_url: str, order_no: str, link) -> tuple[str, str]:
    """"배송조회" 버튼은 새 탭이 아니라 같은 탭에서 결과 페이지로 이동한다.

    다음 버튼을 클릭할 수 있도록, 결과를 읽은 뒤 주문상세 페이지로 되돌아간다.
    """
    link.click()
    elapsed_ms = 0
    while TRACKING_URL_MARKER not in page.url and elapsed_ms < TRACKING_NAV_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(500)
        elapsed_ms += 500
    if TRACKING_URL_MARKER not in page.url:
        raise ParseError("배송조회 클릭 후 결과 페이지로 이동하지 못했습니다.")

    body_text = page.inner_text("body")
    result = _parse_tracking_page(body_text, order_no)
    page.goto(product_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
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


def _scrape_tracking_from_page(page: Page, product_url: str, order_no: str, order_option: str | None) -> TrackingResult:
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
        tracking_no, courier = _click_tracking_link(page, product_url, order_no, links.first)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    body_text = page.inner_text("body")
    matched_idx = _select_link_index_by_order_option(body_text, count, order_option)
    if matched_idx is not None:
        tracking_no, courier = _click_tracking_link(page, product_url, order_no, links.nth(matched_idx))
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 클릭해서 실제로 서로 다른 송장인지 확인한다
    # (다른 어댑터와 동일한 안전 규칙). 클릭할 때마다 주문상세 페이지로 돌아오므로,
    # 매번 버튼을 새로 조회해야 한다(이전 로케이터는 이동한 페이지 기준이라
    # 재사용할 수 없다).
    results = []
    for i in range(count):
        fresh_links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
        results.append(_click_tracking_link(page, product_url, order_no, fresh_links.nth(i)))

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
        page.goto(product_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        if not _looks_authenticated(page):
            if headless:
                raise BlockedError(
                    "CJ온스타일 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            # 주문상세 URL은 비로그인 시 로그인 폼이 아니라 홈으로 조용히
            # 리다이렉트되므로, 로그인 폼이 확실히 뜨는 URL을 거쳐서 로그인한다.
            page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                _prefill_login_id(page)
                _safe_print("[cjonstyle] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호 입력, '사람인지 확인' 체크 후 로그인해주세요.")
                _safe_print("[cjonstyle] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if not _looks_authenticated(page):
                raise BlockedError("로그인 후에도 주문상세 페이지에 접근하지 못했습니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, product_url, order_no, order_option))
    finally:
        page.close()
