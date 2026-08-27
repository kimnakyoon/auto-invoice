"""현대Hmall(현대홈쇼핑) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.hmall.com/mo/mpa/selectOrdPTCPup?ordNo=<주문번호>&selectTypeGbcd=
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고 ordNo만
  있으면 되도록 만들었다.)
- 로그인이 안 되어 있으면 https://www.hmall.com/mo/cob/loginForm 으로 리다이렉트된다.
  로그인 폼 셀렉터: 아이디 "#userid", 비밀번호 "#password".
- 아이디/비밀번호로 완전 자동 로그인(SSG 어댑터와 같은 방식)을 시도해봤으나,
  로그인 버튼 클릭 시 내부적으로 reCAPTCHA v3를 호출하고(api/hf/od/v1/order/
  recaptcha-siteverify) 자동화된 클릭은 낮은 점수(확인된 값: 0.4)를 받아
  "로그인에 실패하였습니다. 다른 로그인 수단을 이용바랍니다."로 항상 막혔다.
  그래서 롯데온/지마켓 등과 동일하게 아이디만 자동 입력하고, 비밀번호 입력과
  로그인 버튼 클릭은 사람이 직접 하도록 만들었다 - 사람이 실제로 클릭하면
  이 문제가 없다. 로그인 세션은 storage_state(쿠키)로 저장되어 다음 실행부터는
  다시 로그인할 필요가 없다(사용자가 원한 "쿠키로 자동 로그인"은 이 방식으로
  충족된다).
- 주문상세 페이지의 "배송조회" 링크(<span>, 클릭 핸들러가 상위 엘리먼트에
  있어 href가 없음)를 클릭하면 새 탭이 아니라 같은 탭에서
  https://www.hmall.com/mo/mpa/selectDlvTrcUrl?wbno=<송장번호>&codename=<택배사명>&...
  로 이동한다. 택배사명(codename)과 송장번호(wbno)가 화면 텍스트가 아니라
  이동한 URL의 쿼리스트링에 그대로 들어있어(URL 인코딩된 한글이지만
  urllib.parse.parse_qs가 자동으로 디코딩함), 화면을 스크래핑할 필요 없이
  이 값만 읽으면 된다.
- 상품이 여러 개라 "배송조회" 링크가 여러 개 뜨는 경우, 샵마인 엑셀의
  "주문옵션" 값으로 어느 링크인지 특정할 수 있으면 그 링크만 클릭한다.
  특정할 수 없으면(무신사/GSSHOP과 동일한 안전 규칙) 전부 클릭해서 실제로
  서로 다른 송장인지 비교하고, 다르면 사람이 확인하도록 예외를 던진다.
  클릭할 때마다 다른 탭이 아니라 같은 탭이 이동해버리므로, 다음 링크를
  클릭하기 전에 주문상세 페이지로 다시 돌아간다.
- 아직 발송 전 상태 문구(NOT_YET_PATTERNS)는 실제 미발송 주문으로 확인한
  적이 없어 다른 어댑터에서 흔히 보이는 값으로 추정해뒀다 - 다르게 나오면
  조정이 필요하다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from ..models import TrackingResult
from .base import BlockedError, ParseError, TrackingNotAvailableYet, normalize_option

load_dotenv()

LOGIN_ID_SELECTOR = "#userid"

DOMAINS = {"hmall.com", "www.hmall.com"}
SITE_KEY = "hmall"

DEFAULT_COURIER = "택배"  # 이동한 URL에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분
TRACKING_NAV_WAIT_TIMEOUT_MS = 5 * 1000  # 배송조회 클릭 후 페이지 이동 대기 최대 5초

TRACKING_LINK_TEXT = "배송조회"
TRACKING_URL_MARKER = "selectDlvTrcUrl"
NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문접수"]

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
    qs = parse_qs(parsed.query)
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page: Page) -> bool:
    page.wait_for_timeout(1500)
    if "login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


def _prefill_login_id(page: Page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다.

    로그인 버튼 자동 클릭도 하지 않는다 - reCAPTCHA v3가 자동화된 클릭에
    낮은 점수를 매겨 로그인이 거부되는 것을 확인했다(위 docstring 참고).
    """
    hmall_id = os.environ.get("HMALL_ID")
    if not hmall_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(hmall_id)
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


def _parse_tracking_url(url: str) -> tuple[str, str]:
    qs = parse_qs(urlparse(url).query)
    wbno_values = qs.get("wbno")
    if not wbno_values:
        raise ParseError(f"배송조회 결과 URL에서 송장번호(wbno)를 찾지 못했습니다: {url}")
    tracking_no = re.sub(r"[^0-9]", "", wbno_values[0])

    codename_values = qs.get("codename")
    courier = _normalize_courier(codename_values[0].strip()) if codename_values and codename_values[0].strip() else DEFAULT_COURIER

    return tracking_no, courier


def _click_tracking_link(page: Page, product_url: str, link) -> tuple[str, str]:
    """"배송조회" 링크는 새 탭이 아니라 같은 탭에서 결과 URL로 이동한다.

    다음 링크를 클릭할 수 있도록, 결과를 읽은 뒤 주문상세 페이지로 되돌아간다.
    """
    link.click()
    elapsed_ms = 0
    while TRACKING_URL_MARKER not in page.url and elapsed_ms < TRACKING_NAV_WAIT_TIMEOUT_MS:
        page.wait_for_timeout(500)
        elapsed_ms += 500
    if TRACKING_URL_MARKER not in page.url:
        raise ParseError("배송조회 클릭 후 결과 페이지로 이동하지 못했습니다.")

    tracking_no, courier = _parse_tracking_url(page.url)
    page.goto(product_url, wait_until="domcontentloaded")
    return tracking_no, courier


def _select_link_index_by_order_option(body_text: str, count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 어느 "배송조회" 링크 근처(상품명/옵션은 그 앞에
    나온다) 텍스트에만 유일하게 나타나면 그 링크의 인덱스를 쓴다. 0개 또는 2개 이상
    매칭되면 None - 호출자가 전부 클릭해서 비교하는 방식으로 넘어간다."""
    if count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    positions = [m.start() for m in re.finditer(re.escape(TRACKING_LINK_TEXT), body_text)]
    if len(positions) != count:
        return None  # 텍스트와 링크 개수가 안 맞으면(예상치 못한 구조) 안전하게 포기
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
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise ParseError(f"화면에서 배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    if count == 1:
        tracking_no, courier = _click_tracking_link(page, product_url, links.first)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    body_text = page.inner_text("body")
    matched_idx = _select_link_index_by_order_option(body_text, count, order_option)
    if matched_idx is not None:
        # 이 페이지의 링크를 순서대로 다시 찾아서(다른 인덱스로 클릭한 적이
        # 없으므로 이 시점에서는 원래 페이지 상태 그대로다) 클릭한다.
        tracking_no, courier = _click_tracking_link(page, product_url, links.nth(matched_idx))
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    # 옵션으로 특정할 수 없으면 전부 클릭해서 실제로 서로 다른 송장인지 확인한다
    # (무신사/GSSHOP 어댑터와 동일한 안전 규칙). 클릭할 때마다 주문상세 페이지로
    # 돌아오므로, 매번 링크를 새로 조회해야 한다(이전 로케이터는 이동한 페이지
    # 기준이라 재사용할 수 없다).
    results = []
    for i in range(count):
        fresh_links = page.get_by_text(TRACKING_LINK_TEXT, exact=True)
        results.append(_click_tracking_link(page, product_url, fresh_links.nth(i)))

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

        if _looks_like_login_page(page):
            if headless:
                raise BlockedError(
                    "현대몰 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[hmall] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[hmall] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        return _scrape_tracking_from_page(page, product_url, order_no, order_option)
    finally:
        page.close()
