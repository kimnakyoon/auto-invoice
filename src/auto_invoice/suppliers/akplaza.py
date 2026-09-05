"""AK플라자(디지털AK플라자, digital-akplaza.com) 공급사 어댑터.

리버스엔지니어링 결과(2026-08-28 실측):
- 주문상세 URL: https://www.digital-akplaza.com/mypage/orderList/<주문번호>
  로그인이 안 되어 있으면 /login?returnUrl=<원래주소> 로 리다이렉트되므로,
  로그인이 필요한지 판단하기 쉽다.
- AKPLAZA_ID/AKPLAZA_PW 환경변수가 있으면 세션 만료 시 사람 개입 없이 완전
  자동으로 재로그인한다(패션플러스/롯데온과 동일한 방식). 로그인 폼은
  아이디 "#m_id", 비밀번호 "#m_pw", 버튼 <button class="btn pg bk">로그인</button>.
  reCAPTCHA/Turnstile 같은 봇 확인도 키보드보안 iframe도 없고, headless
  브라우저로도 로그인과 조회가 모두 정상 동작하는 것을 확인했다.
  - 로그인 결과는 POST /login/doLogin 의 JSON으로 알려준다:
    {"captcha":false,"cert":false,"code":"0000","dorm":false,"pwdChange":false,
     "messageTitle":""} - code "0000"이 성공이고, 나머지 플래그는 각각
    보안문자/추가 본인인증/휴면계정/비밀번호 변경 요구를 뜻한다. 이 응답을
    붙잡아 두면 "로그인이 왜 안 됐는지"를 정확히 말해줄 수 있다.
  - AKPLAZA_PW를 비워두면 아이디만 자동 입력하고 사람이 직접 로그인한다.
- 송장 조회는 화면을 긁을 필요조차 없다. 주문상세의 "배송조회" 링크가
    <a btn-id="deliTrackingBtn" data-invoice_no="598598518843"
       data-deli_tracking_url="https://trace.goodsflow.com/VIEW/V1/whereis/akplaza/CJGLS/598598518843">
  형태라, 링크 속성에 송장번호와 택배사 코드가 그대로 들어있다(클릭해서 팝업을
  띄울 필요가 없다).
- 택배사 **이름**은 goodsflow(배송지키미) API로 확인한다 - 패션플러스와 같은
  3자 배송조회 서비스이고 memberCode만 "akplaza"로 다르다:
    POST https://trace.goodsflow.com/VIEW/api/tracking
    body: {"memberCode":"akplaza","logisticsCode":"CJGLS","invoiceNo":"<송장번호>"}
  응답 baseData.logisticsName 에 "CJ대한통운"처럼 정식 명칭이 들어있다.
  이 API는 CORS를 goodsflow 자체 도메인에서만 허용하지만, Playwright의
  context.request는 브라우저 fetch가 아니라 별도 HTTP 클라이언트라 제약이 없다.
  (주의: 존재하지 않는 송장번호를 넣어도 isSuccess=true로 돌려주므로 송장번호가
  진짜인지 확인하는 용도로는 못 쓴다 - 송장번호는 어디까지나 사이트 DOM에서
  읽은 값을 쓰고, 이 API는 택배사명을 얻는 데만 쓴다.)
  API가 실패하면 택배사 코드로 이름을 유추한다(COURIER_CODE_NAMES).
- 아직 발송 전이면 "배송조회" 링크 자체가 없다.
- 상품이 여러 개라 링크가 여러 개인 경우, 샵마인 엑셀의 "주문옵션" 값으로 몇
  번째 링크인지 특정한다. 특정할 수 없는데 송장번호까지 서로 다르면 사람이
  확인하도록 예외를 던진다(다른 어댑터와 동일한 안전 규칙).
"""

from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    raise_if_delayed,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#m_id"
LOGIN_PW_SELECTOR = "#m_pw"
LOGIN_BUTTON_SELECTOR = "button.btn.pg.bk"
LOGIN_API_PATH = "/login/doLogin"
LOGIN_SUCCESS_CODE = "0000"

DOMAINS = {"digital-akplaza.com", "www.digital-akplaza.com"}
SITE_KEY = "akplaza"
# 요청 간격. 조회가 주문상세 API 호출 하나(0.1~0.3초)라 기본 간격(1.5~4초)이
# 시간의 전부였다 - 4910과 같은 근거로 좁힌다 (2026-09-02).
REQUEST_GAP = (0.5, 1.2)

ORDER_DETAIL_URL = "https://www.digital-akplaza.com/mypage/orderList/{order_no}"

# "배송조회" 링크. 텍스트로 찾지 않고 사이트가 붙여둔 btn-id로 찾는다 - 링크
# 속성(data-invoice_no / data-deli_tracking_url)에 필요한 값이 다 들어있다.
TRACKING_LINK_SELECTOR = "a[btn-id='deliTrackingBtn']"
TRACKING_LINK_TEXT = "배송조회"  # 주문옵션 매칭에 쓸 화면 텍스트

GOODSFLOW_API_URL = "https://trace.goodsflow.com/VIEW/api/tracking"
GOODSFLOW_MEMBER_CODE = "akplaza"

DEFAULT_COURIER = "택배"  # 택배사명을 끝내 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게
LOGIN_RESPONSE_TIMEOUT_MS = 15 * 1000  # 로그인 API 응답 대기

NOT_YET_PATTERNS = ["배송준비중", "상품준비중", "결제완료", "입금대기", "주문접수"]

# goodsflow API가 실패했을 때 택배사 코드로 이름을 유추한다.
COURIER_CODE_NAMES = {
    "CJGLS": "CJ대한통운",
    "KOREX": "CJ대한통운",
    "HANJIN": "한진택배",
    "LOTTE": "롯데택배",
    "HDEXP": "롯데택배",
    "EPOST": "우체국택배",
    "LOGEN": "로젠택배",
    "KDEXP": "경동택배",
}


def extract_order_no(product_url: str) -> str:
    match = re.search(r"/mypage/orderList/(\d+)", product_url)
    if match:
        return match.group(1)
    raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")


def _looks_like_login_page(page) -> bool:
    return common.looks_like_login_page(page, lambda url: "/login" in url.lower(),
                                        needs_password=False)


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("AKPLAZA_ID"))


def _login_failure_reason(response) -> str | None:
    """POST /login/doLogin 응답에서 실패 사유를 읽는다 (성공이면 None).

    응답은 code + 상황별 플래그로 온다. code가 "0000"이 아니면 실패이고,
    플래그가 켜져 있으면 자동으로는 넘길 수 없는 추가 절차를 요구받은 것이라
    사람에게 넘긴다.
    """
    if response is None:
        return None
    if response.status != 200:
        return f"HTTP {response.status}"
    try:
        data = response.json() or {}
    except Exception:
        return None  # 응답을 못 읽으면 아래 화면 상태 확인에 맡긴다

    if data.get("captcha"):
        return "보안문자(캡차)를 요구받았습니다"
    if data.get("cert"):
        return "추가 본인인증을 요구받았습니다"
    if data.get("dorm"):
        return "휴면 계정으로 전환되어 있습니다"
    if data.get("pwdChange"):
        return "비밀번호 변경을 요구받았습니다"

    code = str(data.get("code") or "")
    if code and code != LOGIN_SUCCESS_CODE:
        message = str(data.get("messageTitle") or "").strip()
        return message or f"코드 {code}"
    return None


def _auto_login(page) -> bool:
    """AKPLAZA_ID/AKPLAZA_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    비밀번호가 설정되어 있지 않으면 False를 돌려주고, 호출자가 기존의 수동
    로그인 방식으로 넘어간다 (비밀번호를 저장하고 싶지 않은 경우를 위해 수동
    로그인 경로를 남겨뒀다 - 패션플러스와 동일한 구조).
    """
    login_id = os.environ.get("AKPLAZA_ID")
    login_pw = os.environ.get("AKPLAZA_PW")
    if not login_id or not login_pw:
        return False

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)

    def _is_login_api(response) -> bool:
        return response.request.method == "POST" and LOGIN_API_PATH in response.url

    try:
        with page.expect_response(_is_login_api, timeout=LOGIN_RESPONSE_TIMEOUT_MS) as resp_info:
            page.locator(LOGIN_BUTTON_SELECTOR, has_text="로그인").first.click()
        response = resp_info.value
    except PlaywrightTimeoutError:
        response = None  # 응답을 못 잡아도 아래 화면 상태 확인으로 판정한다

    reason = _login_failure_reason(response)
    if reason:
        raise BlockedError(f"AK플라자 자동 로그인이 거부됐습니다: {reason}")

    elapsed_ms = 0
    while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        if not _looks_like_login_page(page):
            return True
        elapsed_ms += 1500

    raise BlockedError(
        "AK플라자 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 "
        "(--headless 없이 실행해 브라우저 창을 확인해주세요)."
    )


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _logistics_code_from_url(tracking_url: str) -> str | None:
    """.../VIEW/V1/whereis/akplaza/<택배사코드>/<송장번호> 에서 택배사 코드를 꺼낸다."""
    match = re.search(r"/whereis/[^/]+/([^/?#]+)/", tracking_url)
    return match.group(1) if match else None


def _collect_tracking_links(page) -> list[tuple[str, str | None]]:
    """"배송조회" 링크에서 (송장번호, 택배사코드)를 화면 순서 그대로 수집한다."""
    locator = page.locator(TRACKING_LINK_SELECTOR)
    links: list[tuple[str, str | None]] = []
    for i in range(locator.count()):
        el = locator.nth(i)
        invoice_no = (el.get_attribute("data-invoice_no") or "").strip()
        if not invoice_no:
            continue
        tracking_url = el.get_attribute("data-deli_tracking_url") or ""
        links.append((invoice_no, _logistics_code_from_url(tracking_url)))
    return links


def _fetch_courier_name(context: BrowserContext, invoice_no: str, logistics_code: str | None) -> str | None:
    """goodsflow API로 택배사 정식 명칭을 받아온다 (실패하면 None)."""
    if not logistics_code:
        return None
    try:
        resp = context.request.post(
            GOODSFLOW_API_URL,
            data=json.dumps(
                {
                    "memberCode": GOODSFLOW_MEMBER_CODE,
                    "logisticsCode": logistics_code,
                    "invoiceNo": invoice_no,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        if resp.status != 200:
            return None
        data = resp.json()
        if not data.get("isSuccess"):
            return None
        return ((data.get("baseData") or {}).get("logisticsName") or "").strip() or None
    except Exception:
        return None


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
    links = _collect_tracking_links(page)
    if not links:
        body_text = page.inner_text("body")
        if any(p in body_text for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
        raise_if_delayed(body_text, order_no)
        raise_if_cancelled(body_text, order_no)
        raise ParseError(f"배송조회 링크를 찾지 못했습니다 (주문번호={order_no}).")

    chosen: tuple[str, str | None] | None = None
    if len({inv for inv, _ in links}) > 1:
        body_text = page.inner_text("body")
        matched_idx = _select_by_order_option(body_text, len(links), order_option)
        if matched_idx is None:
            raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다.")
        chosen = links[matched_idx]
    else:
        # 상품이 여러 개라도 같은 박스로 묶여 나가면 송장번호가 같다 - 그때는
        # 그 값을 대표로 쓴다(패션플러스 어댑터와 동일한 규칙).
        chosen = links[0]

    invoice_no, logistics_code = chosen
    tracking_no = re.sub(r"[^0-9]", "", invoice_no)
    if not tracking_no:
        raise ParseError(f"배송조회 링크의 송장번호를 읽지 못했습니다 (주문번호={order_no}).")

    courier_name = _fetch_courier_name(context, invoice_no, logistics_code)
    if not courier_name and logistics_code:
        courier_name = COURIER_CODE_NAMES.get(logistics_code.upper())
    courier = common.normalize_courier((courier_name or "").strip() or DEFAULT_COURIER)

    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(ORDER_DETAIL_URL.format(order_no=order_no), wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            if _auto_login(page):
                common.safe_print("[akplaza] 로그인 세션이 없어 자동 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "AK플라자 로그인이 필요합니다. .env에 AKPLAZA_PW를 넣으면 자동 로그인하고, "
                    "비밀번호를 저장하지 않으려면 --headless 없이 실행해 직접 로그인해주세요."
                )
            else:
                _prefill_login_id(page)
                common.safe_print("[akplaza] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[akplaza] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            page.goto(ORDER_DETAIL_URL.format(order_no=order_no), wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 화면이 아직 덜 그려진 채로 읽으면 '아직 미발급'으로 잘못 넘길 수 있다
        # (조용히 틀리는 쪽이라 특히 위험하다). 그 주문의 주문번호가 화면에
        # 뜨면 다 그려진 것이다 - '배송조회' 같은 글자는 상단 메뉴에도 있어서
        # 표식으로 쓰면 덜 그려진 화면을 다 그려진 것으로 볼 수 있다.
        common.wait_for_text(page, order_no, common.ORDER_RENDER_WAIT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(context, page, order_no, order_option))
    finally:
        page.close()
