"""무신사(MUSINSA) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.musinsa.com/order/order-detail/<주문번호>
  (주문 목록 https://www.musinsa.com/order/order-list 의 "주문 상세" 링크와 동일.
  이 어댑터는 이 페이지 자체는 스크래핑하지 않고 로그인 여부 확인에만 쓴다.)
- 처음엔 화면의 "배송 조회" 버튼을 Playwright로 클릭해 새 탭(팝업)에서
  택배사/송장번호를 읽으려 했으나, 실제 크롬(claude-in-chrome 확장)에서는
  버튼을 누르면 새 탭이 정상적으로 열리는 반면 Playwright Chromium에서는
  headless/headed 둘 다 팝업 자체가 열리지 않았다(자동화 브라우저를
  구분해서 팝업을 막는 것으로 보임). 대신 그 버튼이 내부적으로 호출하는
  JSON API를 직접 호출하는 방식으로 바꿨다:
    GET https://www.musinsa.com/order-service/my/order/get_order_view/<주문번호>
  이 API는 화면과 완전히 동일한 세션 쿠키로 인증되고(별도 페이지 이동 없이
  context.request로 바로 호출 가능), 응답 JSON의
  orderList.orderOptionList[].deliveryCompanyCode / deliveryInvoiceNo 에
  택배사 코드와 송장번호가 그대로 들어있다. 화면 스크래핑보다 훨씬
  안정적이라 이 방식을 기본으로 쓴다.
  - 로그인이 안 되어 있으면 이 API가 200과 함께 로그인 페이지 HTML을
    돌려준다(JSON이 아님) - content-type으로 판별한다.
  - 존재하지 않거나 이 계정 소유가 아닌 주문번호면
    {"result": "INVALID_DATA", ...} 를 돌려준다.
  - 아직 발송 전이면 deliveryCompanyCode/deliveryInvoiceNo가 둘 다 null이다.
- 로그인이 안 되어 있으면(위 API로 판별) member.one.musinsa.com/login 으로
  리다이렉트되는 실제 화면을 띄워서 로그인을 유도한다. 로그인 폼은
  placeholder "통합계정 또는 이메일" / "비밀번호"로 되어 있다.
- MUSINSA_PW/MUSINSA_PW2/MUSINSA_PW3가 있으면 세션이 끊겼을 때 사람 개입 없이
  완전 자동으로 재로그인한다(롯데온/패션플러스/롯데아이몰과 동일한 방식).
  로그인 페이지를 실측해 확인한 것(2026-08-28):
  - Vue로 그려지는 폼이라 id/name이 없다 - 입력창은 placeholder로, 로그인
    버튼은 폼 안의 submit 버튼으로 잡는다(폼 안의 나머지 버튼은 비밀번호
    보기 눈 아이콘 하나뿐이라 type=submit으로 충분히 구분된다).
  - 아이디/비밀번호는 폼 제출 직전에 페이지 JS가 암호화해서 hidden 필드
    (encryptMemberId/encryptPassword)에 넣는다. 그래서 hidden 필드를 직접
    채우면 안 되고, 반드시 보이는 입력창에 넣고 버튼을 눌러야 한다.
  - 봇 확인(reCAPTCHA)은 평소 꺼져 있다(hidden 필드 isCheckGoogleRecaptcha가
    "false"). 로그인 실패가 반복되면 켜지는 것으로 보이는데, 켜져 있으면
    자동 로그인을 포기하고 사람에게 넘긴다(억지로 뚫지 않는다).
  - 로그인 실패는 화면 문구가 아니라 alert()으로 온다(롯데온/롯데아이몰과
    같다) - 예: "아이디 또는 패스워드를 확인하세요." Playwright는 핸들러가
    없으면 alert을 조용히 닫아버려 실패를 감지하지 못하므로 dialog 핸들러로
    그 문구를 받아 실패 사유째로 올린다.
  - "자동 로그인" 체크박스를 켜면 세션이 훨씬 오래 유지된다 - 로그인 횟수를
    줄이면 봇 확인이 켜질 일도 줄어드니 자동 로그인 때는 켜고 들어간다.
    체크박스 자체는 CSS로 숨겨져 있어(class="blind") 짝이 되는 label을
    클릭해야 한다.
  - 이 사이트는 headless 브라우저로도 로그인 페이지가 정상적으로 뜬다
    (롯데아이몰과 달리 403으로 막지 않는다).
  MUSINSA_PW를 비워두면 예전처럼 아이디만 자동 입력하고 사람이 직접 로그인한다.
- 크롬에 이미 로그인되어 있는 세션의 쿠키를 그대로 가져와 쓰는 방식
  (scripts/import_chrome_session.py)은 무신사에서는 통하지 않았다 -
  Cloudflare로 보이는 봇 차단이 있어서, 쿠키만 다른 브라우저(Playwright
  Chromium)로 옮기면 서버가 세션을 무효로 처리했다(로그인 페이지로
  리다이렉트됨). 그래서 무신사는 롯데온/지마켓/네이버와 동일하게, 이
  어댑터(또는 scripts/musinsa_login_setup.py)가 직접 띄우는 Playwright
  브라우저 안에서 최초 1회 사람이 직접 로그인해야 한다. 그 브라우저의
  storage_state는 이후에도 계속 유효하다(같은 브라우저 엔진으로 발급된
  세션이라 문제 없음).
- 사용자가 계정을 3개 쓰고 있어(각각 다른 주문을 구매), 어느 계정에 특정
  주문이 있는지 미리 알 수 없다. API가 돌려주는 INVALID_DATA로 "이 계정
  소유가 아님(또는 존재하지 않음)"을 판별해서 다음 계정으로 넘어간다
  (네이버 어댑터의 2계정 순환 로직과 동일한 패턴을 3계정으로 확장했다).
  세 계정은 완전히 별도의 BrowserContext(별도 storage_state 파일:
  auth/musinsa_state.json, auth/musinsa2_state.json,
  auth/musinsa3_state.json)로 관리한다 - 오케스트레이터는 SITE_KEY
  "musinsa" 하나만 알고 첫 번째 계정용 context만 만들어주므로, 두 번째/세
  번째 계정용 context는 이 모듈이 내부적으로 만들고 로그인 직후 직접
  storage_state를 저장한다.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
)

load_dotenv()

LOGIN_ID_PLACEHOLDER = "통합계정 또는 이메일"
LOGIN_PW_PLACEHOLDER = "비밀번호"
# Vue가 그리는 폼이라 버튼에 id가 없다. 폼 안의 나머지 버튼은 "비밀번호 보기" 눈
# 아이콘(type=button)뿐이라 type=submit으로 로그인 버튼을 정확히 짚을 수 있다.
LOGIN_SUBMIT_SELECTOR = "#loginForm button[type=submit]"
# "자동 로그인" 체크박스는 class="blind"로 숨겨져 있어 직접 못 누른다 - label을 누른다.
AUTOLOGIN_CHECKBOX_SELECTOR = "#login-v2-member__util__login-auto"
AUTOLOGIN_LABEL_SELECTOR = "label[for='login-v2-member__util__login-auto']"
# 봇 확인(reCAPTCHA) 사용 여부를 담고 있는 hidden 필드. 평소에는 "false"다.
RECAPTCHA_FLAG_SELECTOR = "#isCheckGoogleRecaptcha"

DOMAINS = {"musinsa.com", "www.musinsa.com"}
SITE_KEY = "musinsa"

SECOND_ACCOUNT_STATE_KEY = "musinsa2"
THIRD_ACCOUNT_STATE_KEY = "musinsa3"

# 계정번호 -> (storage_state 키, 아이디 환경변수, 비밀번호 환경변수)
ACCOUNTS = {
    "1": (SITE_KEY, "MUSINSA_ID", "MUSINSA_PW"),
    "2": (SECOND_ACCOUNT_STATE_KEY, "MUSINSA_ID2", "MUSINSA_PW2"),
    "3": (THIRD_ACCOUNT_STATE_KEY, "MUSINSA_ID3", "MUSINSA_PW3"),
}

ORDER_DETAIL_URL = "https://www.musinsa.com/order/order-detail/{order_no}"
ORDER_VIEW_API_URL = "https://www.musinsa.com/order-service/my/order/get_order_view/{order_no}"

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게

# 무신사가 쓰는 택배사 코드(스마트택배 표준 코드와 동일 체계로 보임) -> 정식 명칭.
# 확인된 건 CJGLS/LOTTE 뿐이고 나머지는 흔히 쓰이는 코드를 추정으로 채워뒀다 -
# 매핑에 없는 코드는 원래 코드를 그대로 쓴다(잘못 변환하는 것보다 안전).
COURIER_CODE_MAP = {
    "CJGLS": "CJ대한통운",
    "LOTTE": "롯데택배",
    "HANJIN": "한진택배",
    "KDEXP": "경동택배",
    "EPOST": "우체국택배",
    "LOGEN": "로젠택배",
    "HDEXP": "합동택배",
    "CVSNET": "GS Postbox 편의점택배",
    "DELIBOX": "딜리박스",
}

# 두 번째/세 번째 계정용 context 캐시. GUI는 같은 파이썬 프로세스 안에서 실행
# 버튼을 여러 번 누를 수 있고 그때마다 orchestrator가 새 Browser를 여니,
# 브라우저 인스턴스별로 캐시해야 지난 실행에서 이미 닫힌 context를 재사용하지
# 않는다.
_extra_context_cache: dict[tuple[int, str], BrowserContext] = {}


def _normalize_courier(raw: str) -> str:
    """무신사만 택배사를 코드("CJGLS")로 준다 - 코드표로 먼저 풀고 나서
    공통 정규화(축약형 -> 정식 명칭)를 태운다. 코드표에 없으면 이미 한글
    명칭으로 온 것으로 보고 그대로 넘긴다."""
    return common.normalize_courier(COURIER_CODE_MAP.get(raw, raw))


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if not segments or not segments[-1].isdigit():
        raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")
    return segments[-1]


def _fetch_order_view(context: BrowserContext, order_no: str) -> dict | None:
    """로그인이 안 되어 있으면 None을 반환한다(API가 JSON 대신 로그인 페이지
    HTML을 돌려줌)."""
    resp = context.request.get(ORDER_VIEW_API_URL.format(order_no=order_no))
    content_type = resp.headers.get("content-type", "")
    if "json" not in content_type:
        return None
    return resp.json()


def _looks_like_login_page(page) -> bool:
    """비밀번호 입력창 유무로도 판단할 수 있지만, 무신사 로그인 SPA가 타이핑
    중 순간적으로 다시 그려져 count()가 잠깐 0이 되는 경우가 있어 신뢰할 수
    없다(네이버 어댑터와 동일한 이유). 로그인 성공 시 반드시
    member.one.musinsa.com을 벗어나므로 URL만으로 판단한다."""
    # 무신사는 자바스크립트로 로그인 화면에 넘긴다(실측: goto 직후에는 아직
    # 주문상세 주소) - 그래서 이 사이트는 주소가 바뀌는지 잠깐 지켜본다.
    return common.looks_like_login_page(
        page, lambda url: "member.one.musinsa.com" in url, needs_password=False,
        settle_ms=common.LOGIN_REDIRECT_SETTLE_MS)


def _prefill_login_id(page, musinsa_id: str | None) -> None:
    """수동 로그인 폴백용 - 아이디만 채워서 사람이 칠 것을 비밀번호만 남긴다."""
    common.prefill_login_id(page, page.get_by_placeholder(LOGIN_ID_PLACEHOLDER), musinsa_id)


def _recaptcha_required(page) -> bool:
    """봇 확인(reCAPTCHA)이 켜져 있는지 본다.

    평소에는 hidden 필드 isCheckGoogleRecaptcha가 "false"고, 로그인 실패가
    반복되면 켜지는 것으로 보인다. 켜져 있으면 자동 로그인을 시도하지 않고
    사람에게 넘긴다.
    """
    locator = page.locator(RECAPTCHA_FLAG_SELECTOR)
    if locator.count() == 0:
        return False
    try:
        return (locator.first.get_attribute("value") or "").strip().lower() == "true"
    except Exception:
        return False


def _check_autologin(page) -> None:
    """"자동 로그인"을 켜서 세션이 오래 가게 한다 (best effort).

    체크박스가 class="blind"로 숨겨져 있어 Playwright의 check()로는 못 누른다 -
    짝이 되는 label을 클릭해서 토글한다. 실패해도 로그인 자체에는 지장이
    없으니 조용히 넘어간다.
    """
    try:
        checkbox = page.locator(AUTOLOGIN_CHECKBOX_SELECTOR)
        if checkbox.count() == 0 or checkbox.first.is_checked():
            return
        page.locator(AUTOLOGIN_LABEL_SELECTOR).first.click()
    except Exception:
        pass


def _auto_login(page, account_label: str) -> bool:
    """계정별 MUSINSA_ID/MUSINSA_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 계정을 위해 수동
    로그인 경로를 그대로 남겨뒀다).

    롯데온/롯데아이몰 어댑터와 같은 패턴이다 - 이 사이트도 로그인 실패를 화면
    문구가 아니라 alert()으로 알려주기 때문에 dialog 핸들러로 그 문구를 받아
    실패 사유째로 올린다. 핸들러가 없으면 Playwright가 alert을 조용히
    닫아버려서 원인도 모른 채 대기 시간만 다 쓰고 실패한다.
    """
    _, id_env, pw_env = ACCOUNTS[account_label]
    login_id = os.environ.get(id_env)
    login_pw = os.environ.get(pw_env)
    if not login_id or not login_pw:
        return False

    if _recaptcha_required(page):
        raise BlockedError(
            f"무신사({account_label})가 봇 확인(reCAPTCHA)을 요구하고 있어 자동 로그인을 할 수 없습니다 "
            "- 뜬 브라우저 창에서 직접 로그인해주세요."
        )

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    try:
        # 아이디/비밀번호는 폼 제출 직전에 페이지 JS가 암호화해서 hidden 필드에
        # 넣는다 - 반드시 보이는 입력창에 넣고 버튼을 눌러야 한다.
        page.get_by_placeholder(LOGIN_ID_PLACEHOLDER).fill(login_id)
        page.get_by_placeholder(LOGIN_PW_PLACEHOLDER, exact=True).fill(login_pw)
        _check_autologin(page)
        page.locator(LOGIN_SUBMIT_SELECTOR).first.click()

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1200)
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인 자체는
            # 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를 alert보다
            # 먼저 본다 (롯데온/롯데아이몰과 같은 이유).
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"무신사({account_label}) 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1200

        if _recaptcha_required(page):
            raise BlockedError(
                f"무신사({account_label})가 로그인 도중 봇 확인(reCAPTCHA)을 요구했습니다 "
                "- 뜬 브라우저 창에서 직접 로그인해주세요."
            )
        raise BlockedError(
            f"무신사({account_label}) 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
            "(추가 본인인증을 요구받았을 수 있습니다 - 브라우저 창을 확인해주세요)."
        )
    finally:
        page.remove_listener("dialog", _on_dialog)


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS, poll_ms=1200)


def _get_extra_context(primary_context: BrowserContext, state_key: str, headless: bool) -> BrowserContext:
    browser = primary_context.browser
    if browser is None:  # pragma: no cover - 방어적 코드, 실제로는 항상 browser가 있음
        raise BlockedError(f"무신사({state_key})용 브라우저를 준비하지 못했습니다.")

    cache_key = (id(browser), state_key)
    cached = _extra_context_cache.get(cache_key)
    if cached is not None:
        return cached

    state_path = browser_mod.state_path(state_key)
    if state_path.exists():
        context = browser.new_context(storage_state=str(state_path))
    else:
        context = browser.new_context()
    _extra_context_cache[cache_key] = context
    return context


def _state_key_for(account_label: str) -> str:
    return ACCOUNTS[account_label][0]


def _ensure_logged_in(context: BrowserContext, order_no: str, headless: bool, account_label: str) -> None:
    """API가 로그인 필요를 감지했을 때만 호출된다. 실제 로그인에 성공하면
    storage_state를 저장한다."""
    _, id_env, pw_env = ACCOUNTS[account_label]
    if headless and not os.environ.get(pw_env):
        raise BlockedError(
            f"무신사 로그인이 필요합니다({account_label}). .env에 {pw_env}를 넣어두면 자동으로 "
            "로그인합니다. 아니면 --headless 없이 실행해 수동으로 로그인하거나, "
            "scripts/musinsa_login_setup.py로 미리 로그인해주세요."
        )

    page = context.new_page()
    try:
        page.goto(ORDER_DETAIL_URL.format(order_no=order_no), wait_until="domcontentloaded")
        if not _looks_like_login_page(page):
            return  # 이미 로그인되어 있었음 (레이스 컨디션 등 방어)

        if _auto_login(page, account_label):
            common.safe_print(f"[musinsa] ({account_label}) 로그인 세션이 없어 자동 로그인했습니다.")
        else:
            if headless:  # pragma: no cover - 위에서 이미 걸러지지만 방어적으로 남긴다
                raise BlockedError(f"무신사 로그인이 필요합니다({account_label}). --headless 없이 실행해주세요.")
            _prefill_login_id(page, os.environ.get(id_env))
            common.safe_print(f"[musinsa] ({account_label}) 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            common.safe_print(f"[musinsa] ({account_label}) 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not _wait_for_manual_login(page):
                raise BlockedError(f"무신사({account_label}) 로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")

        context.storage_state(path=str(browser_mod.state_path(_state_key_for(account_label))))
    finally:
        page.close()


def _find_by_order_option(shipped: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션" 값으로 상품을 정확히 짚을 수 있으면 그걸
    쓴다 - 여러 상품 중 어느 것의 송장인지 추측할 필요가 없어져 가장
    정확하다. 매칭이 0개(표기가 서로 안 맞음)거나 2개 이상(값이 너무
    짧아 여러 옵션에 다 들어있는 등 애매함)이면 None을 반환해서 호출자가
    기존 방식(개수 비교 + 상품준비중 우선)으로 넘어가게 한다."""
    if len(shipped) <= 1 or not order_option:
        return None

    target = normalize_option(order_option)
    if not target:
        return None

    matched = [
        opt
        for opt in shipped
        if target in normalize_option(opt.get("goodsOption")) or target in normalize_option(opt.get("originGoodsOption"))
    ]
    return matched[0] if len(matched) == 1 else None


def _tracking_from_order_view(data: dict, order_no: str, order_option: str | None = None) -> TrackingResult:
    order_list = data.get("orderList") or {}
    options = order_list.get("orderOptionList") or []
    if not options:
        raise ParseError(f"주문 응답에 상품 정보가 없습니다 (주문번호={order_no}).")

    shipped = [opt for opt in options if opt.get("deliveryInvoiceNo")]
    if not shipped:
        status_text = options[0].get("orderStateText", "알 수 없음")
        # 주문상태를 정확히 읽을 수 있으니 취소/품절 판정을 먼저 한다.
        raise_if_cancelled(status_text, order_no)
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={status_text}).")

    matched_opt = _find_by_order_option(shipped, order_option)
    if matched_opt is not None:
        courier = _normalize_courier(str(matched_opt.get("deliveryCompanyCode") or "").strip())
        return TrackingResult(tracking_no=matched_opt["deliveryInvoiceNo"], courier=courier)

    tracking_nos = {opt["deliveryInvoiceNo"] for opt in shipped}
    if len(tracking_nos) > 1 and len(options) != len(tracking_nos):
        # 상품 개수와 송장 개수가 다르면 아직 일부만 발송된 것인지, 일부 상품이
        # 같은 박스로 묶여 나간 것인지 텍스트만으로는 구분할 수 없다 - 안전하게
        # 사람이 확인하게 한다. 반면 개수가 정확히 같으면(상품별로 각자 다른
        # 송장 하나씩, 빠짐없이 전부 발급됨) 완전히 발송 처리된 것으로 보고
        # 대표로 하나만 반영한다 (샵마인 업로드 형식이 주문당 송장 1개만 지원).
        raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")

    # 여러 상품이 각자 다른 송장을 받았을 때 대표로 하나를 골라야 하는데,
    # "상품 준비 중"인 것도 이미 송장번호가 배정되어 있는 경우가 실제로
    # 있었다(택배사가 라벨을 미리 발급해두고 아직 안 걷어간 상태로 보임).
    # 이런 상품은 아직 안 나갔을 가능성이 커서, 있으면 그걸 우선으로 쓴다
    # (나중에 실제로 걷어갈 때 이 송장번호로 바로 조회가 되니 더 유용하다).
    preparing = next((opt for opt in shipped if "준비중" in opt.get("orderStateText", "").replace(" ", "")), None)
    opt = preparing if preparing is not None else shipped[0]
    courier = _normalize_courier(str(opt.get("deliveryCompanyCode") or "").strip())
    return TrackingResult(tracking_no=opt["deliveryInvoiceNo"], courier=courier)


def _get_tracking_from_account(
    context: BrowserContext,
    order_no: str,
    headless: bool,
    account_label: str,
    order_option: str | None,
) -> TrackingResult:
    data = _fetch_order_view(context, order_no)
    if data is None:
        _ensure_logged_in(context, order_no, headless, account_label)
        data = _fetch_order_view(context, order_no)
        if data is None:
            raise BlockedError(f"무신사({account_label}) 로그인 후에도 여전히 로그인이 필요합니다.")

    if data.get("result") != "SUCCESS":
        raise OrderNotFound(f"이 계정({account_label})에서 주문을 찾을 수 없습니다 (주문번호={order_no}).")

    # 이 어댑터는 주문상세 화면을 열지 않고 API 응답만 보므로, 주문일도
    # 그 JSON에서 읽는다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
    return attach_order_date(
        order_date_mod.from_json(data),
        lambda: _tracking_from_order_view(data, order_no, order_option),
    )


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)

    try:
        return _get_tracking_from_account(context, order_no, headless, "1", order_option)
    except OrderNotFound:
        pass

    second_context = _get_extra_context(context, SECOND_ACCOUNT_STATE_KEY, headless)
    try:
        return _get_tracking_from_account(second_context, order_no, headless, "2", order_option)
    except OrderNotFound:
        pass

    third_context = _get_extra_context(context, THIRD_ACCOUNT_STATE_KEY, headless)
    return _get_tracking_from_account(third_context, order_no, headless, "3", order_option)
