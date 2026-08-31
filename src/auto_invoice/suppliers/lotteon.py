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
- 로그인은 SSG/더현대/NS홈쇼핑/11번가/옥션과 동일하게 완전 자동 로그인이
  가능하다(사용자 요청으로 확인 후 도입). 실제로 확인한 것:
  로그인 페이지는 /p/member/login/common?rtnUrl=<원래주소> 이고, reCAPTCHA도
  키보드보안 플러그인도 없다. 존재하지 않는 아이디로 제출해보니 로그인 API
  (POST pbf.lotteon.com/member/v1/auth/loginDivision)가 HTTP 200으로 정상
  응답했다 - 위의 주문상세 API와 달리 로그인 경로는 Imperva가 막지 않는다.
  단, 로그인 실패가 화면 문구가 아니라 자바스크립트 alert()로 뜬다
  ("일치하는 회원정보가 없습니다."). Playwright는 핸들러가 없으면 alert을
  조용히 닫아버려서, 핸들러를 걸지 않으면 실패를 감지하지 못하고 타임아웃까지
  기다리게 된다. 그래서 이 어댑터만 dialog 핸들러를 쓴다.
  (비밀번호는 페이지 JS가 세션별 키로 암호화해 members.lpoint.com으로 보내므로,
  브라우저 없이 requests로 직접 로그인하는 방식은 쓸 수 없다.)
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#inId"
LOGIN_PW_SELECTOR = "#Password"
LOGIN_BUTTON_TEXT = "로그인하기"

DOMAINS = {"lotteon.com", "www.lotteon.com"}
SITE_KEY = "lotteon"

DEFAULT_COURIER = "롯데택배"  # 화면에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초

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
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("LOTTEON_ID"))


def _auto_login(page) -> bool:
    """LOTTEON_ID/LOTTEON_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    SSG/더현대/NS홈쇼핑/11번가/옥션 어댑터와 같은 패턴이지만, 롯데온은 로그인
    실패를 화면 문구가 아니라 alert()으로 알려주기 때문에 dialog 핸들러로 그
    문구를 받아 실패 사유째로 올린다. 핸들러가 없으면 Playwright가 alert을
    조용히 닫아버려서, 원인도 모른 채 대기 시간만 다 쓰고 실패한다.

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 경우를 위해
    수동 로그인 경로를 그대로 남겨뒀다).
    """
    login_id = os.environ.get("LOTTEON_ID")
    login_pw = os.environ.get("LOTTEON_PW")
    if not login_id or not login_pw:
        return False

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    try:
        page.fill(LOGIN_ID_SELECTOR, login_id)
        page.fill(LOGIN_PW_SELECTOR, login_pw)
        page.get_by_text(LOGIN_BUTTON_TEXT, exact=True).first.click()

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인
            # 자체가 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를
            # alert보다 먼저 본다.
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"롯데온 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1500  # _looks_like_login_page 내부에서 1500ms 대기함

        raise BlockedError(
            "롯데온 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
            "(추가 본인인증을 요구받았을 수 있습니다 - 브라우저 창을 확인해주세요)."
        )
    finally:
        page.remove_listener("dialog", _on_dialog)


def _wait_for_manual_login(page) -> bool:
    """비밀번호 입력창이 사라질 때까지(=로그인 완료) 화면 상태를 폴링하며 대기한다.

    GUI(pythonw)로 실행할 때는 콘솔이 없어 input()으로 "로그인 후 Enter"를
    받을 수 없다 (stdin이 없어 예외가 나거나 그대로 멈춘다). 그래서 사람이
    직접 로그인 버튼을 눌러 페이지가 바뀌는 것을 감지하는 방식으로 바꿨다.
    """
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


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


def _select_by_order_option(body_text: str, matches: list, order_option: str | None):
    """샵마인 엑셀의 "주문옵션" 값이 어느 송장번호 근처(보통 상품명/옵션은
    송장번호보다 앞에 나온다) 텍스트에만 유일하게 나타나면 그 매치를 쓴다.
    0개(표기가 안 맞음) 또는 2개 이상(애매함) 매칭되면 None - 호출자가
    기존 방식(사람 확인 요청)으로 넘어간다."""
    if len(matches) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    candidates = []
    prev_end = 0
    for m in matches:
        # window 시작을 이전 매치 끝 이후로 묶어서, 앞 상품의 옵션 텍스트가
        # 다음 상품 판단에 섞여 들어가지(bleed) 않게 한다.
        window = body_text[max(prev_end, m.start() - 400) : m.start()]
        if target in normalize_option(window):
            candidates.append(m)
        prev_end = m.end()
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(page, od_no: str, order_option: str | None = None) -> TrackingResult:
    _click_tracking_button(page)

    body_text = page.inner_text("body")

    tracking_matches = list(TRACKING_NO_PATTERN.finditer(body_text))
    if not tracking_matches:
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (odNo={od_no}).")
        raise_if_cancelled(body_text, od_no)
        raise ParseError(f"화면에서 송장번호 텍스트를 찾지 못했습니다 (odNo={od_no}).")

    distinct_tracking_nos = {re.sub(r"[^0-9]", "", m.group(1)) for m in tracking_matches}
    tracking_match = _select_by_order_option(body_text, tracking_matches, order_option)
    if tracking_match is None:
        if len(distinct_tracking_nos) > 1:
            # 한 주문이 상품별로 나눠 배송되어 서로 다른 송장번호가 여러 개
            # 보이는 경우다 - 어느 걸 써야 하는지 확신할 수 없어 사람이
            # 확인하게 한다 (무신사 어댑터와 동일한 안전 규칙).
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (odNo={od_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
        tracking_match = tracking_matches[0]
    tracking_no = re.sub(r"[^0-9]", "", tracking_match.group(1))

    # "택배사"는 페이지 여기저기(예: "상품이 없을 경우 택배사에 문의해 주세요")에도
    # 등장해서 body_text 전체에서 찾으면 엉뚱한 문장을 잡을 수 있다. 실제 택배사명은
    # 항상 송장번호 바로 앞에 "택배사\n롯데택배" 형태로 나오므로, 그 근처만 본다.
    window_start = max(0, tracking_match.start() - 60)
    window = body_text[window_start : tracking_match.start()]
    courier_match = COURIER_PATTERN.search(window)
    courier = common.normalize_courier(courier_match.group(1).strip()) if courier_match else DEFAULT_COURIER

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    od_no = extract_od_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if _auto_login(page):
                common.safe_print("[lotteon] 로그인 세션이 없어 자동 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "롯데온 로그인이 필요합니다. .env에 LOTTEON_PW를 넣으면 자동 로그인하고, "
                    "비밀번호를 저장하지 않으려면 --headless 없이 실행해 직접 로그인해주세요."
                )
            else:
                _prefill_login_id(page)
                common.safe_print("[lotteon] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[lotteon] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, od_no, order_option))
    finally:
        page.close()
