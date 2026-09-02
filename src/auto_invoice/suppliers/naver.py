"""네이버페이(Naver Pay) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://orders.pay.naver.com/order/status/<주문번호>
  (구매내역 목록의 "주문 상세 보기" 링크와 동일. 목록 페이지
  https://pay.naver.com/pc/history 자체는 주문 식별 정보가 없어 조회에
  쓸 수 없다.)
- NAVER_PW/NAVER_PW2가 있으면 세션이 끊겼을 때 사람 개입 없이 완전 자동으로
  재로그인한다. 오래 "네이버는 자동 로그인 불가"로 두었던 사이트인데,
  2026-08-28에 CJ온스타일을 뚫은 방법으로 다시 시도해 뒤집었다. 갈린 것은
  **크롬을 누가 실행했는지**였다(같은 날 대조 실험으로 확인):
    * 번들 Chromium(평소 조회에 쓰는 컨텍스트, navigator.webdriver=true)
      -> 아이디/비밀번호를 실제 키 입력으로 한 글자씩 쳐 넣어도 로그인 버튼을
         누르는 즉시 "보안을 위해 추가 확인을 해주세요" 캡차(가상 영수증의
         빈칸 채우기)가 떴다.
    * 우리가 직접 실행한 크롬에 CDP로 붙기(browser.real_chrome_cdp_context,
      navigator.webdriver=false) -> 캡차 없이 그대로 로그인됐다. 계정 2개를
      각각 새 프로필로 시도해 둘 다 성공했다.
  즉 입력 방식이 아니라 브라우저를 띄우는 방식이 원인이었다(현대몰과 같은
  결론, 자세한 배경은 browser.py의 두 함수 docstring). 그래서 로그인만 그
  크롬 창에서 하고 쿠키만 원래 컨텍스트로 옮겨온 뒤, 조회는 평소대로
  headless로 이어간다 - 현대몰/CJ온스타일과 같은 구조다. 사람이 타이핑하거나
  체크박스를 누를 일은 없다.
- 비밀번호 칸은 그래도 page.fill()이 아니라 실제 키 입력(press_sequentially)으로
  채운다. 네이버 로그인 폼은 타이핑 패턴을 hidden 필드에 담아 함께 보내는데,
  한 번에 값만 꽂아 넣으면 그 필드가 비어서 굳이 위험을 만들 이유가 없다.
- 캡차가 그래도 뜨면(계정별로 이미 의심 상태일 수 있다) 통과를 시도하지 않고
  즉시 포기해 사람에게 넘긴다 - 억지로 뚫지 않는다는 다른 어댑터와 같은 규칙.
  NAVER_PW를 비워두면 예전처럼 아이디만 자동 입력하고 사람이 직접 로그인한다.
- 로그인 폼의 "로그인 상태 유지"(#loginStay)는 자동/수동 어느 쪽이든 켜둔다.
  기본값이 꺼짐이라 그냥 두면 NID_AUT/NID_SES가 만료일 없는 세션 쿠키로만
  저장된다(실측). 켜두면 장기 쿠키로 내려와 재로그인 주기가 길어진다. 매
  실행마다 최신 쿠키를 storage_state에 다시 저장해 세션을 이어가는 것도 그대로다.
  이미 크롬에 네이버 로그인이 되어 있다면 scripts/import_chrome_session.py로
  그 세션을 그대로 가져올 수도 있다.
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
  계정용 context는 이 모듈이 내부적으로 만들고 storage_state도 직접
  저장한다(로그인 직후만이 아니라 조회에 성공할 때마다 - 오케스트레이터의
  실행 끝 저장이 이 context에는 적용되지 않기 때문이다).
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
from . import common
from .base import (
    BlockedError,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#id"
# 로그인 폼의 "로그인 상태 유지" 체크박스(기본값 꺼짐). 켜두면 로그인 쿠키가
# 세션 쿠키(브라우저를 닫으면 소멸)가 아니라 만료일이 있는 장기 쿠키로 내려와서,
# 사람이 직접 로그인해야 하는 횟수가 크게 줄어든다.
KEEP_LOGIN_SELECTOR = "#loginStay"
LOGIN_PW_SELECTOR = "#pw"
# 로그인 버튼. button[type=submit]은 화면에 안 보이는 언어선택 버튼들이라
# 쓸 수 없다. 레이아웃에 따라 _row/_column 중 하나만 보인다.
LOGIN_BUTTON_SELECTOR = "#loginBtn_row, #loginBtn_column"
AUTO_LOGIN_WAIT_TIMEOUT_MS = 20 * 1000
# 자동 로그인이 막혔을 때 뜨는 추가 확인(캡차) 화면의 문구.
CAPTCHA_PATTERNS = ["보안을 위해", "추가 확인", "자동입력 방지", "가장 비싼"]

DOMAINS = {"orders.pay.naver.com"}
SITE_KEY = "naver"

SECOND_ACCOUNT_STATE_KEY = "naver2"

# 계정별 비밀번호 환경변수. 비워두면 그 계정은 예전처럼 사람이 직접 로그인한다.
PW_ENV_BY_ACCOUNT = {"1": "NAVER_PW", "2": "NAVER_PW2"}

# 자동 로그인용 크롬 프로필. 계정마다 따로 둬야 한 프로필에 두 계정이 섞이지 않는다
# (auth/chrome_profile_naver, auth/chrome_profile_naver2).
CHROME_PROFILE_BY_ACCOUNT = {"1": SITE_KEY, "2": SECOND_ACCOUNT_STATE_KEY}

# 로그인 여부를 확인하려고 여는 페이지. 로그인이 없으면 nid.naver.com으로 넘어간다.
LOGIN_CHECK_URL = "https://pay.naver.com/pc/history"

# 로그인 창 크기. 기본(창 크기 그대로)으로 두지 않고 실측에 성공한 값으로 고정한다.
LOGIN_VIEWPORT = {"width": 1280, "height": 900}

# 한 글자당 입력 간격 - 사람이 치는 속도에 가깝게 둔다.
TYPING_DELAY_MS = 120

ORDER_STATUS_URL = (
    "https://orders.pay.naver.com/order/status/{order_no}"
    "?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"
)

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 로그인 대기 최대 5분

TRACK_BUTTON_TEXT = "배송조회"

COURIER_TRACKING_PATTERN = re.compile(r"([가-힣A-Za-z0-9()]{2,20})\n송장번호\n([0-9][0-9\-]{5,})")
# "취소"는 여기 있었지만 뺐다 - 취소된 주문은 기다려도 송장이 안 나와서
# '아직 미발급'으로 묶어두면 매 실행마다 조용히 스킵되며 영영 남는다.
# 이제 raise_if_cancelled가 취소/품절로 따로 분류해 결과 정리에 올린다.
NOT_YET_PATTERNS = ["상품준비중", "결제완료", "배송준비중", "발송준비"]

# 주문상세가 그려지기를 기다리는 최대 시간 (_redirected_or_rendered).
DETAIL_RENDER_TIMEOUT_MS = 2000
TRACKING_RENDER_TIMEOUT_MS = 2000  # [배송조회] 누른 뒤 송장번호가 뜰 때까지 (예전 고정 대기와 같은 크기)
# 다른 계정의 주문이라 구매내역 목록으로 튕기는지 지켜보는 시간 (_redirected_or_rendered).
REDIRECT_SETTLE_MS = 1200  # 예전 고정 대기(1.2초)와 같은 크기

_second_context_cache: dict[int, BrowserContext] = {}


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
    return common.looks_like_login_page(
        page, lambda url: "nid.naver.com" in url, needs_password=False)




def _prefill_login_id(page, naver_id: str | None) -> None:
    """사람이 직접 로그인할 때(자동 로그인이 막혔거나 비밀번호가 없을 때)
    아이디만 미리 채워 타이핑을 줄인다. 비밀번호는 이 창에서는 채우지 않는다 -
    자동 로그인은 별도의 크롬 창에서만 하기 때문이다(_auto_login)."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), naver_id)


def _enable_keep_login(page) -> None:
    """사람이 로그인하기 전에 "로그인 상태 유지"를 켜둔다.

    이 체크박스는 네이버가 사용자에게 제공하는 평범한 옵션이고, 켜두면 로그인
    쿠키가 장기 쿠키로 내려와서 사람이 다시 로그인해야 하는 주기가 길어진다.
    (실측: 꺼진 상태로 로그인하면 NID_AUT/NID_SES가 전부 만료일 없는 세션
    쿠키로 저장된다.) 체크박스가 없거나 이미 켜져 있으면 아무것도 하지 않는다.
    """
    locator = page.locator(KEEP_LOGIN_SELECTOR)
    if locator.count() == 0:
        return
    try:
        if not locator.first.is_checked():
            # input 자체가 화면에서 숨겨져 있고 label이 실제로 보이는 형태라
            # check()가 아니라 label 클릭으로 켠다.
            locator.first.check(force=True)
    except Exception:
        pass


def _looks_like_captcha(page) -> bool:
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    return any(p in body_text for p in CAPTCHA_PATTERNS)


def _auto_login(context: BrowserContext, naver_id_env: str, account_label: str) -> bool:
    """NAVER_ID/NAVER_PW로 자동 로그인하고, 받은 쿠키를 원래 컨텍스트에 옮긴다.

    로그인은 우리가 직접 실행한 크롬 창(browser.real_chrome_cdp_context)에서만
    캡차 없이 통과한다 - 이유와 대조 실험은 이 파일 맨 위 docstring 참고.
    조회까지 그 창에서 하지는 않고, 현대몰/CJ온스타일과 마찬가지로 쿠키만
    원래 컨텍스트로 옮긴다.

    비밀번호가 없거나 캡차가 뜨면 False를 돌려주고, 호출자가 기존 수동 로그인
    경로로 넘어간다.
    """
    login_id = os.environ.get(naver_id_env)
    login_pw = os.environ.get(PW_ENV_BY_ACCOUNT.get(account_label, ""))
    if not login_id or not login_pw:
        return False

    profile_key = CHROME_PROFILE_BY_ACCOUNT.get(account_label, SITE_KEY)
    try:
        with browser_mod.real_chrome_cdp_context(profile_key) as login_context:
            page = login_context.pages[0] if login_context.pages else login_context.new_page()
            page.set_viewport_size(LOGIN_VIEWPORT)

            page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            if not _looks_like_login_page(page):
                # 이 프로필에 로그인이 남아 있으면 쿠키만 옮기고 끝낸다.
                context.add_cookies(login_context.cookies())
                return True

            if page.locator(LOGIN_ID_SELECTOR).count() == 0:
                common.safe_print(f"[naver] ({account_label}) 로그인 페이지에서 아이디 입력창을 찾지 못했습니다.")
                return False

            # page.fill이 아니라 실제 키 입력으로 채운다 (docstring 참고).
            page.locator(LOGIN_ID_SELECTOR).click()
            page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=TYPING_DELAY_MS)
            page.wait_for_timeout(400)
            page.locator(LOGIN_PW_SELECTOR).click()
            page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=TYPING_DELAY_MS)
            page.wait_for_timeout(600)
            _enable_keep_login(page)
            page.locator(LOGIN_BUTTON_SELECTOR).locator("visible=true").first.click()

            elapsed_ms = 0
            while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
                # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
                # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
                page.wait_for_timeout(1200)
                if not _looks_like_login_page(page):
                    context.add_cookies(login_context.cookies())
                    return True
                if _looks_like_captcha(page):
                    common.safe_print(f"[naver] ({account_label}) 추가 확인(캡차)이 떠서 자동 로그인을 중단합니다.")
                    return False
                elapsed_ms += 1200
            common.safe_print(f"[naver] ({account_label}) 자동 로그인이 시간 안에 끝나지 않았습니다.")
            return False
    except Exception as exc:
        common.safe_print(f"[naver] ({account_label}) 자동 로그인 중 오류({exc}) - 직접 로그인으로 넘어갑니다.")
        return False


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


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


def _ensure_logged_in(
    page, context: BrowserContext, headless: bool, naver_id_env: str, account_label: str
) -> bool:
    """로그인을 실제로 수행했으면 True를 반환한다 (호출자가 이때만
    storage_state를 저장하면 된다).

    자동 로그인은 headless 여부와 무관하게 시도한다 - 로그인 창은 이 함수가
    쓰는 크롬이 따로 띄우고, 조회는 넘겨받은 컨텍스트에서 그대로 이어진다.
    """
    if not _looks_like_login_page(page):
        return False

    if _auto_login(context, naver_id_env, account_label):
        common.safe_print(f"[naver] ({account_label}) 로그인 세션이 없어 자동 로그인했습니다.")
        return True

    if headless:
        raise BlockedError(
            f"네이버페이 로그인이 필요합니다({account_label}). "
            f".env에 {PW_ENV_BY_ACCOUNT.get(account_label, 'NAVER_PW')}를 넣으면 자동 로그인을 시도하고, "
            "막히면 --headless 없이 실행해 직접 로그인해주세요."
        )
    _prefill_login_id(page, os.environ.get(naver_id_env))
    _enable_keep_login(page)
    common.safe_print(f"[naver] ({account_label}) 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호 입력과 보안 확인을 완료해주세요.")
    common.safe_print(f"[naver] ({account_label}) 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
    if not _wait_for_manual_login(page):
        raise BlockedError(f"네이버페이({account_label}) 로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
    return True


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




def _redirected_or_rendered(page) -> str:
    """"redirected"(다른 계정 주문이라 목록으로 튕김) / "rendered"([배송조회]
    버튼이 붙음 = 내 주문이고 발송됨) / "settled"(튕김 대기 시간 안에 아무 일도
    없음 = 내 주문인데 아직 발송 전이거나 느림).

    튕김은 goto 뒤 1초쯤 걸리는 자바스크립트 리다이렉트라(2026-08-31 실측) 그 시간(REDIRECT_SETTLE_MS)은 반드시 지켜본다. 다만 그 안에 상세가
    먼저 그려지면 내 주문이 확실하니 바로 넘어간다. 버튼이 없는 주문(발송 전)은
    예전처럼 DETAIL_RENDER_TIMEOUT_MS까지 기다려 본 뒤 아래 판정에 맡긴다.
    """
    button = page.get_by_text(TRACK_BUTTON_TEXT, exact=True).first
    waited_ms = 0
    poll_ms = 100
    while waited_ms < max(REDIRECT_SETTLE_MS, DETAIL_RENDER_TIMEOUT_MS):
        if "orders.pay.naver.com" not in page.url:
            return "redirected"
        try:
            if button.count() > 0:
                return "rendered"
        except Exception:  # noqa: BLE001 - 다시 그리는 중이면 다음 바퀴에 본다
            pass
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms
    return "settled"


def _scrape_tracking_from_page(page, order_no: str, order_option: str | None = None) -> TrackingResult:
    button = page.get_by_text(TRACK_BUTTON_TEXT, exact=True)
    if button.count() == 0:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"배송조회 버튼을 찾지 못했습니다 (주문번호={order_no}).")

    try:
        button.first.click(timeout=3000)
    except Exception as e:
        raise ParseError(f"배송조회 버튼 클릭에 실패했습니다 (주문번호={order_no}): {e}") from e
    # 송장번호가 화면에 뜨는 순간까지만 기다린다 - 예전에는 무조건 2초를 잤다
    # (롯데온과 같은 개선, 2026-09-02). 끝내 안 뜨면 예전과 같은 2초를 채운다.
    body_text = common.wait_for_match(
        page, lambda: page.inner_text("body"), COURIER_TRACKING_PATTERN,
        timeout_ms=TRACKING_RENDER_TIMEOUT_MS)
    matches = list(COURIER_TRACKING_PATTERN.finditer(body_text))
    if not matches:
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"화면에서 송장번호 텍스트를 찾지 못했습니다 (주문번호={order_no}).")

    distinct_tracking_nos = {re.sub(r"[^0-9]", "", m.group(2)) for m in matches}
    match = _select_by_order_option(body_text, matches, order_option)
    if match is None:
        if len(distinct_tracking_nos) > 1:
            # 한 주문이 상품별로 나눠 배송되어 서로 다른 송장번호가 여러 개
            # 보이는 경우다 - 어느 걸 써야 하는지 확신할 수 없어 사람이
            # 확인하게 한다 (무신사 어댑터와 동일한 안전 규칙).
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
        match = matches[0]
    courier = common.normalize_courier(match.group(1).strip())
    tracking_no = re.sub(r"[^0-9]", "", match.group(2))

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def _get_tracking_from_account(
    context: BrowserContext,
    order_no: str,
    headless: bool,
    naver_id_env: str,
    account_label: str,
    order_option: str | None,
) -> TrackingResult:
    page = context.new_page()
    logged_in = False
    try:
        url = ORDER_STATUS_URL.format(order_no=order_no)
        page.goto(url, wait_until="domcontentloaded")

        did_login = _ensure_logged_in(page, context, headless, naver_id_env, account_label)
        # 여기까지 왔다면 로그인된 상태다(_ensure_logged_in은 실패 시 예외를
        # 던진다). finally의 세션 저장은 이 플래그가 켜졌을 때만 한다 -
        # 로그인에 실패한 채로 저장하면 살아있던 세션 파일을 로그아웃
        # 상태로 덮어쓴다.
        logged_in = True
        if did_login:
            # 네이버는 로그인 성공 시 원래 요청했던 URL로 자동 복귀하지만,
            # 혹시 그대로 로그인 페이지에 남아있으면 한 번 더 이동을 시도한다.
            if _looks_like_login_page(page):
                page.goto(url, wait_until="domcontentloaded")
                if _looks_like_login_page(page):
                    raise BlockedError(f"네이버페이({account_label}) 로그인 후에도 여전히 로그인 페이지입니다.")
            state_path = browser_mod.state_path(SITE_KEY if account_label == "1" else SECOND_ACCOUNT_STATE_KEY)
            context.storage_state(path=str(state_path))

        # '다른 계정 주문이라 목록으로 튕기는가'와 '주문상세가 그려졌는가'를
        # 한 번에 지켜본다 - 예전에는 튕김 대기 1.2초를 다 채운 뒤에야 렌더를
        # 기다려서, 내 주문일수록 1.2초를 통째로 버렸다(2026-09-02).
        if _redirected_or_rendered(page) == "redirected":
            raise OrderNotFound(f"이 계정({account_label})에서 주문을 찾을 수 없습니다 (주문번호={order_no}).")
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        # 두 번째 계정 세션은 여기서 직접 저장해야 한다. 오케스트레이터는
        # supplier_contexts에 담긴 context만 실행 끝에 저장하는데, 두 번째
        # 계정용 context는 이 모듈이 내부적으로 만든 거라 거기에 없다. 그래서
        # 예전에는 "사람이 수동 로그인한 순간"에만 저장됐고, 그 뒤로 네이버가
        # 쿠키를 회전시켜도 파일에 반영되지 않아 1번 계정보다 훨씬 빨리 만료돼
        # 수동 로그인을 다시 요구했다.
        # finally에 두는 이유: 주문이 취소됐거나(OrderCancelled) 아직 송장이
        # 없거나(TrackingNotAvailableYet) 이 계정 소유가 아니어도
        # (OrderNotFound) 쿠키는 이미 갱신된 상태라 저장해두는 게 맞다.
        if account_label == "2" and logged_in:
            try:
                context.storage_state(path=str(browser_mod.state_path(SECOND_ACCOUNT_STATE_KEY)))
            except Exception:
                pass
        page.close()


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)

    try:
        return _get_tracking_from_account(context, order_no, headless, "NAVER_ID", "1", order_option)
    except OrderNotFound:
        pass

    second_context = _get_second_context(context, headless)
    return _get_tracking_from_account(second_context, order_no, headless, "NAVER_ID2", "2", order_option)
