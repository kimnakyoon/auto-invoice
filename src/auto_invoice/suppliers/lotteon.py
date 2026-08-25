"""롯데온(LotteOn) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.lotteon.com/p/order/claim/orderDetail?odNo=<odNo>
- 실제 송장 조회 API(POST pbf.lotteon.com/order/claim/v1/mylotte/getOrderDetail)를
  찾긴 했지만, 로그인된 브라우저 안에서 page.evaluate()로 인페이지 fetch를 여러 번
  호출하니 Imperva가 이 호출 패턴 자체를 의심스러운 자동화로 보고 막기 시작했다
  (페이지 자체는 정상 로그인 상태로 잘 보이는데도 API만 HTTP 999로 거부됨).
  반면 화면에서 "배송조회" 버튼을 사람이 직접 클릭하는 건 항상 정상 동작했다.
  그래서 API를 직접 호출하지 않고, Playwright로 실제 버튼 클릭을 흉내내서
  화면에 렌더링된 텍스트(택배사/송장번호)를 읽는 방식으로 바꿨다. 이게 진짜
  사용자 행동과 동일해서 차단 가능성이 훨씬 낮다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import BlockedError, ParseError, TrackingNotAvailableYet

load_dotenv()

LOGIN_ID_SELECTOR = "#inId"

DOMAINS = {"lotteon.com", "www.lotteon.com"}
SITE_KEY = "lotteon"

DEFAULT_COURIER = "롯데택배"  # 화면에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

# 배송정보 상세를 여는 버튼 텍스트들 (실제 확인된 텍스트: "배송상세조회")
TRACKING_BUTTON_TEXTS = ["배송상세조회", "배송조회", "배송 조회", "송장조회", "배송추적"]

COURIER_PATTERN = re.compile(r"택배사\n([^\n]{1,20})")
TRACKING_NO_PATTERN = re.compile(r"(?:송장번호|운송장번호)\n([0-9][0-9\-]{5,})")
NOT_YET_PATTERNS = ["상품준비중", "배송준비중", "결제완료"]


def extract_od_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("odNo")
    if not values:
        raise ParseError(f"URL에서 odNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page) -> bool:
    """URL만으로는 오탐이 잦아서(로그인 후에도 잠깐 거치는 리다이렉트 URL에
    "login"이 들어있는 경우가 있음), 최종적으로 자리잡은 URL + 실제 비밀번호
    입력창 존재 여부를 함께 본다.
    """
    # 이 페이지는 백그라운드 통신이 끊이지 않아 networkidle이 끝까지 오지 않는다.
    # 그냥 잠깐 대기해서 리다이렉트가 자리잡을 시간만 준다.
    page.wait_for_timeout(1500)

    if "login" not in page.url.lower():
        return False
    return page.locator("input[type='password']").count() > 0


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    lotteon_id = os.environ.get("LOTTEON_ID")
    if not lotteon_id:
        return
    locator = page.locator(LOGIN_ID_SELECTOR)
    if locator.count() == 0:
        return
    try:
        locator.fill(lotteon_id)
    except Exception:
        pass


def _safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:
        pass


def _wait_for_manual_login(page) -> bool:
    """비밀번호 입력창이 사라질 때까지(=로그인 완료) 화면 상태를 폴링하며 대기한다.

    GUI(pythonw)로 실행할 때는 콘솔이 없어 input()으로 "로그인 후 Enter"를
    받을 수 없다 (stdin이 없어 예외가 나거나 그대로 멈춘다). 그래서 사람이
    직접 로그인 버튼을 눌러 페이지가 바뀌는 것을 감지하는 방식으로 바꿨다.
    """
    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함
    return False


def _click_tracking_button(page) -> None:
    for text in TRACKING_BUTTON_TEXTS:
        loc = page.get_by_text(text, exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=3000)
            page.wait_for_timeout(1500)  # 모달/패널 렌더링 대기
            return
        except Exception:
            continue
    # 버튼을 못 찾아도 예외로 바로 끊지 않는다 - 일부 주문은 버튼 없이
    # 페이지에 바로 송장번호가 보이는 경우가 있어, 아래 텍스트 스캔에 맡긴다.


def _scrape_tracking_from_page(page, od_no: str) -> TrackingResult:
    _click_tracking_button(page)

    body_text = page.inner_text("body")

    tracking_match = TRACKING_NO_PATTERN.search(body_text)
    if not tracking_match:
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (odNo={od_no}).")
        raise ParseError(f"화면에서 송장번호 텍스트를 찾지 못했습니다 (odNo={od_no}).")

    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    # "택배사"는 페이지 여기저기(예: "상품이 없을 경우 택배사에 문의해 주세요")에도
    # 등장해서 body_text 전체에서 찾으면 엉뚱한 문장을 잡을 수 있다. 실제 택배사명은
    # 항상 송장번호 바로 앞에 "택배사\n롯데택배" 형태로 나오므로, 그 근처만 본다.
    window_start = max(0, tracking_match.start() - 60)
    window = body_text[window_start : tracking_match.start()]
    courier_match = COURIER_PATTERN.search(window)
    courier = courier_match.group(1).strip() if courier_match else DEFAULT_COURIER

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(context: BrowserContext, product_url: str, headless: bool = True) -> TrackingResult:
    od_no = extract_od_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if headless:
                raise BlockedError(
                    "롯데온 로그인이 필요합니다. 먼저 --headless 없이 실행해 수동으로 로그인해주세요."
                )
            _prefill_login_id(page)
            _safe_print("[lotteon] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            _safe_print("[lotteon] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        return _scrape_tracking_from_page(page, od_no)
    finally:
        page.close()
