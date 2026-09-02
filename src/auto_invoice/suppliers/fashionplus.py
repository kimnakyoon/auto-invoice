"""패션플러스(FASHION PLUS) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.fashionplus.co.kr/mypage/order/detail/<주문번호>
- 로그인 폼은 id="login_id" 입력창(Vue 앱)을 쓴다. 로그인이 안 되어 있으면
  보호된 페이지 접근 시 /auth/login 으로 리다이렉트된다.
- FASHIONPLUS_ID/FASHIONPLUS_PW 환경변수가 있으면 세션 만료 시 사람 개입 없이
  완전 자동으로 재로그인한다(SSG/더현대/NS홈쇼핑/11번가/옥션과 동일한 방식).
  로그인 페이지를 실측해 확인한 결과(2026-08-28) reCAPTCHA/Turnstile 같은 봇
  확인 스크립트도, 키보드보안 iframe도 전혀 없었다. FASHIONPLUS_PW를 비워두면
  예전처럼 아이디만 자동 입력하고 사람이 직접 로그인하는 방식으로 동작한다.
  - 비밀번호 입력창은 id/name이 없어(class="textfield"만 있음) 로그인 폼 안의
    input[type=password]로 찾는다 - 페이지 전체에 하나뿐인 것을 확인했다.
  - 로그인 실패는 alert이 아니라 POST /auth/login 응답으로 알려준다: 401 +
    {"message": "아이디 또는 비밀번호를 잘못 입력하셨습니다."} 형태라, 이
    응답을 붙잡아 실패 사유째로 올린다(화면에도 같은 문구가 토스트로 뜨지만
    응답 쪽이 훨씬 확실하다).
  - 로그인 전에 "로그인 상태 유지" 체크박스를 켜둔다(기본값 꺼짐). 자동/수동
    로그인 양쪽 다 켜서 재로그인 주기를 늘린다(네이버 어댑터와 같은 이유).
- 각 상품 옆 "배송조회" 링크는 새 창으로 열리는 <a href> 링크이고, href가
  바로 https://trace.goodsflow.com/VIEW/V1/whereis/fashionplus/<주문번호>-<상품순번>
  형태다. goodsflow(배송지키미)는 패션플러스와 별개인 3자 배송조회 서비스로,
  로그인/세션 쿠키가 전혀 필요 없는 공개 조회 페이지다 - 그래서 새 탭을 열어
  화면 텍스트를 스크래핑하는 대신(택배사명이 <img alt="한진택배"> 형태라 화면
  텍스트만으로는 못 읽는다), 그 페이지가 내부적으로 호출하는 JSON API를
  context.request로 직접 호출한다:
    POST https://trace.goodsflow.com/VIEW/api/tracking
    body: {"memberCode": "fashionplus", "uniqueCode": "<주문번호>-<상품순번>"}
  응답의 baseData.logisticsName / baseData.invoiceNo 에 택배사/송장번호가
  그대로 들어있다. 이 사이트가 CORS를 프론트엔드(goodsflow 자체 도메인)에서만
  허용해서 패션플러스 페이지에서 직접 fetch하면 막히지만, Playwright의
  context.request(브라우저 fetch가 아니라 별도 HTTP 클라이언트)는 CORS
  제약이 없어 문제없이 호출된다(무신사 어댑터와 동일한 이유).
- 상품순번(-1, -2, ...)은 화면에 상품이 나열된 순서와 같다. 주문상세
  페이지에서 "배송조회" 링크를 DOM 순서대로 모두 수집해서, 몇 번째 링크인지로
  샵마인 "주문옵션" 값과 매칭한다(롯데온/네이버 어댑터와 동일한 패턴).
  실제로 확인한 사례로, 한 주문에 상품이 2개라도 같은 박스로 묶여 나가면
  두 상품 다 같은 송장번호를 돌려주기도 한다 - 이 경우는 서로 다른 척 하지
  않고 그냥 그 값을 대표로 쓴다(다른 어댑터의 "송장번호 개수 비교" 규칙과 동일).
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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

LOGIN_ID_SELECTOR = "#login_id"
# 비밀번호 입력창에는 id/name이 없다 - 로그인 폼(#login_form) 안의 password 타입으로 찾는다.
LOGIN_PW_SELECTOR = "#login_form input[type='password']"
LOGIN_BUTTON_SELECTOR = "#login_form button.mm_btn.__btn_lg_primary__"
# input 자체는 화면에서 숨겨져 있고(커스텀 스타일) label만 보이는 형태다.
KEEP_LOGIN_SELECTOR = "#login_form label.mm_form-check:has-text('로그인 상태 유지') input[type='checkbox']"
LOGIN_API_PATH = "/auth/login"

DOMAINS = {"fashionplus.co.kr", "www.fashionplus.co.kr"}
SITE_KEY = "fashionplus"
# 요청 간격. 조회가 주문상세 API 호출 하나(0.1~0.3초)라 기본 간격(1.5~4초)이
# 시간의 전부였다 - 4910과 같은 근거로 좁힌다 (2026-09-02).
REQUEST_GAP = (0.5, 1.2)

ORDER_DETAIL_URL = "https://www.fashionplus.co.kr/mypage/order/detail/{order_no}"

GOODSFLOW_API_URL = "https://trace.goodsflow.com/VIEW/api/tracking"
GOODSFLOW_MEMBER_CODE = "fashionplus"

DEFAULT_COURIER = "택배"  # goodsflow 응답에 택배사명이 비어있을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게
LOGIN_RESPONSE_TIMEOUT_MS = 15 * 1000  # 로그인 API 응답 대기

TRACKING_LINK_TEXT = "배송조회"
NOT_YET_PATTERNS = ["배송준비중", "결제완료", "입금대기", "주문확인중"]


def extract_order_no(product_url: str) -> str:
    match = re.search(r"/order/detail/(\d+)", product_url)
    if match:
        return match.group(1)
    parsed = urlparse(product_url)
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[-1].isdigit():
        return segments[-1]
    raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")


def _looks_like_login_page(page) -> bool:
    return common.looks_like_login_page(page, lambda url: "/auth/login" in url, needs_password=False)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("FASHIONPLUS_ID"))


def _enable_keep_login(page) -> None:
    """로그인하기 전에 "로그인 상태 유지"를 켜둔다 (기본값은 꺼짐).

    사이트가 사용자에게 정상적으로 제공하는 옵션이고, 켜두면 로그인 쿠키가
    오래 유지돼 재로그인 주기가 길어진다(네이버 어댑터와 같은 이유).
    체크박스가 없거나 이미 켜져 있으면 아무것도 하지 않는다.
    """
    locator = page.locator(KEEP_LOGIN_SELECTOR)
    if locator.count() == 0:
        return
    try:
        if not locator.first.is_checked():
            # input이 화면에서 숨겨져 있어(label만 보임) force=True가 필요하다.
            locator.first.check(force=True)
    except Exception:
        pass


def _auto_login(page) -> bool:
    """FASHIONPLUS_ID/FASHIONPLUS_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 경우를 위해 수동
    로그인 경로를 그대로 남겨뒀다).

    로그인 실패는 POST /auth/login의 401 응답으로 판별해 사유째로 올린다 -
    화면만 보고 있으면 "로그인 페이지에서 안 벗어남"으로만 보여서, 비밀번호가
    틀린 건지 추가 인증이 필요한 건지 알 수 없다.
    """
    login_id = os.environ.get("FASHIONPLUS_ID")
    login_pw = os.environ.get("FASHIONPLUS_PW")
    if not login_id or not login_pw:
        return False

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    _enable_keep_login(page)

    def _is_login_api(response) -> bool:
        return response.request.method == "POST" and LOGIN_API_PATH in response.url

    try:
        with page.expect_response(_is_login_api, timeout=LOGIN_RESPONSE_TIMEOUT_MS) as resp_info:
            page.locator(LOGIN_BUTTON_SELECTOR).first.click()
        response = resp_info.value
    except PlaywrightTimeoutError:
        response = None  # 응답을 못 잡아도 아래 화면 상태 확인으로 판정한다

    if response is not None and response.status != 200:
        raise BlockedError(f"패션플러스 자동 로그인이 거부됐습니다: {_login_error_message(response)}")

    elapsed_ms = 0
    while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500

    raise BlockedError(
        "패션플러스 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
        "(추가 본인인증을 요구받았을 수 있습니다 - --headless 없이 실행해 브라우저 창을 확인해주세요)."
    )


def _login_error_message(response) -> str:
    """로그인 실패 응답({"message": "..."})에서 사유 문구를 꺼낸다."""
    try:
        message = (response.json() or {}).get("message")
    except Exception:
        message = None
    return (message or "").strip() or f"HTTP {response.status}"


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _collect_tracking_links(page) -> list[str]:
    """"배송조회" 링크의 href를 화면에 나온 순서 그대로 수집한다.

    화면에서 "배송조회" 텍스트는 <a> 안의 <span>에 들어있어(get_by_text로
    찾으면 href가 없는 그 span이 잡힌다), 반드시 <a> 태그 자체를 찾아야 한다.
    """
    locator = page.locator(f"a:has-text('{TRACKING_LINK_TEXT}')")
    count = locator.count()
    hrefs = []
    for i in range(count):
        href = locator.nth(i).get_attribute("href")
        if href:
            hrefs.append(href)
    return hrefs


def _unique_code_from_href(href: str) -> str | None:
    match = re.search(r"/whereis/[^/]+/([^/?#]+)", href)
    return match.group(1) if match else None


def _fetch_goodsflow_tracking(context: BrowserContext, unique_code: str) -> dict | None:
    resp = context.request.post(
        GOODSFLOW_API_URL,
        data=json.dumps({"memberCode": GOODSFLOW_MEMBER_CODE, "uniqueCode": unique_code}),
        headers={"Content-Type": "application/json"},
    )
    if resp.status != 200:
        return None
    data = resp.json()
    if not data.get("isSuccess"):
        return None
    return data.get("baseData")


def _select_by_order_option(body_text: str, link_count: int, order_option: str | None) -> int | None:
    """샵마인 엑셀의 "주문옵션" 값이 몇 번째 "배송조회" 링크 앞 텍스트에만
    유일하게 나타나면 그 인덱스를 쓴다. 0개(표기가 안 맞음) 또는 2개 이상
    (애매함) 매칭되면 None - 호출자가 기존 방식(개수 비교)으로 넘어간다."""
    if link_count <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None

    positions = [m.start() for m in re.finditer(re.escape(TRACKING_LINK_TEXT), body_text)]
    if len(positions) != link_count:
        return None  # 텍스트와 링크 개수가 안 맞으면(예상치 못한 구조) 안전하게 포기

    candidates = []
    prev_end = 0
    for idx, pos in enumerate(positions):
        window = body_text[max(prev_end, pos - 400) : pos]
        if target in normalize_option(window):
            candidates.append(idx)
        prev_end = pos
    return candidates[0] if len(candidates) == 1 else None


def _scrape_tracking_from_page(
    context: BrowserContext, page, order_no: str, order_option: str | None = None
) -> TrackingResult:
    hrefs = _collect_tracking_links(page)
    if not hrefs:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    tracked: list[tuple[str, dict]] = []
    for href in hrefs:
        unique_code = _unique_code_from_href(href)
        if unique_code is None:
            continue
        base_data = _fetch_goodsflow_tracking(context, unique_code)
        if base_data and base_data.get("invoiceNo"):
            tracked.append((unique_code, base_data))

    if not tracked:
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")

    distinct_tracking_nos = {t[1]["invoiceNo"] for t in tracked}
    chosen: dict | None = None
    if len(distinct_tracking_nos) > 1:
        body_text = page.inner_text("body")
        matched_idx = _select_by_order_option(body_text, len(hrefs), order_option)
        if matched_idx is not None and matched_idx < len(tracked):
            chosen = tracked[matched_idx][1]
        else:
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
    else:
        chosen = tracked[0][1]

    tracking_no = re.sub(r"[^0-9]", "", chosen["invoiceNo"])
    courier = common.normalize_courier((chosen.get("logisticsName") or "").strip() or DEFAULT_COURIER)
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    url = ORDER_DETAIL_URL.format(order_no=order_no)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if _auto_login(page):
                common.safe_print("[fashionplus] 로그인 세션이 없어 자동 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "패션플러스 로그인이 필요합니다. .env에 FASHIONPLUS_PW를 넣으면 자동 로그인하고, "
                    "비밀번호를 저장하지 않으려면 --headless 없이 실행해 직접 로그인해주세요."
                )
            else:
                _prefill_login_id(page)
                _enable_keep_login(page)
                common.safe_print("[fashionplus] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[fashionplus] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 화면이 아직 덜 그려진 채로 읽으면 '아직 미발급'으로 잘못 넘길 수 있다
        # (조용히 틀리는 쪽이라 특히 위험하다). [배송조회]든 진행중 상태 문구든
        # 판단에 쓸 것이 하나라도 보일 때까지만 기다린다 - 보통은 이미 있어서
        # 그냥 지나간다.
        common.wait_for_text(page, [TRACKING_LINK_TEXT, *NOT_YET_PATTERNS],
                             common.ORDER_RENDER_WAIT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()
