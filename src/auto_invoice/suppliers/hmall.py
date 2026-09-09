"""현대Hmall(현대홈쇼핑) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세 URL: https://www.hmall.com/mo/mpa/selectOrdPTCPup?ordNo=<주문번호>&selectTypeGbcd=
  (샵마인 엑셀의 "상품URL" 컬럼에 이 형태의 URL이 들어있을 것으로 보고 ordNo만
  있으면 되도록 만들었다.)
- 로그인이 안 되어 있으면 https://www.hmall.com/mo/cob/loginForm 으로 리다이렉트된다.
  로그인 폼 셀렉터: 아이디 "#userid", 비밀번호 "#password".
- 로그인 폼은 제출할 때 reCAPTCHA v3를 호출한다(api/hf/od/v1/order/
  recaptcha-siteverify). 처음에는 자동화된 클릭이 낮은 점수(0.4)를 받아
  "로그인에 실패하였습니다. 다른 로그인 수단을 이용바랍니다."로 막혔는데,
  원인은 자동화 자체가 아니라 **브라우저를 띄우는 방식**이었다(2026-08-28 실측).
  점수는 이렇게 갈렸다:
    * 번들 Chromium + 매번 새 빈 컨텍스트(기존 방식) -> 0.4, 거부
    * 설치된 진짜 크롬 + 재사용되는 프로필(창 띄움) -> **0.8, 통과**
    * 설치된 진짜 크롬 + headless -> 0.1, 거부
  그래서 로그인만 browser.real_chrome_context()로 띄운 진짜 크롬 창에서
  하고(GSSHOP과 같은 "로그인만 별도 컨텍스트, 쿠키만 이식" 구조), 성공하면
  쿠키를 원래 컨텍스트로 옮겨 조회는 지금까지처럼 headless로 이어간다
  (실크롬에서 만든 세션이 번들 Chromium headless에서 그대로 통하는 것을
  실제 주문상세까지 열어서 확인했다). 로그인 창이 반드시 보여야 하는 것만
  롯데아이몰과 같은 제약이고, 사람이 타이핑할 일은 없다.
  HMALL_PW가 비어 있으면 예전처럼 아이디만 자동 입력하고 사람이 직접
  로그인하는 경로로 넘어간다. 로그인 세션은 storage_state(쿠키)로 저장되어
  다음 실행부터는 다시 로그인할 필요가 없다.
- HMALL_ID는 더현대(hi.thehyundai.com) 계정과 같은 통합회원이라 비밀번호도
  같다(THEHYUNDAI_PW와 동일한 값으로 로그인되는 것을 확인했다). 다만 쿠키는
  공유되지 않아서(더현대 세션으로 현대몰에 가면 그대로 로그인 페이지가 뜬다)
  사이트별로 각각 로그인해야 한다.
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
- 아직 발송 전이면 "배송조회" 링크가 없고 상품 칸의 상태(<div class="pdstate">)에
  **"상품준비"**(뒤에 "중"이 없다)라고 뜬다 (2026-09-09 실측, 주문상세
  __NEXT_DATA__의 lastOrdStatGbcdNm="상품준비"·lastOrdStatGbcd="25"·invoice="N").
  처음에는 다른 어댑터처럼 "상품준비중"으로 추정해 둬서 이 주문이 스킵이 아니라
  실패("배송조회 링크를 찾지 못했습니다")로 잡혔다. 화면 하단 안내문에는
  "상품준비중"이라는 글자가 숨은 <li>로 항상 들어있는데, page.inner_text는
  숨은 요소를 빼고 주기 때문에(Playwright innerText - Context7로 확인) 그
  글자로 오판하지는 않는다 - text_content로 바꾸면 안 된다.

2026-09-09 최적화 - 화면을 열지 않고 요청 두 번으로 끝낸다:
- 주문상세는 Next.js 서버 렌더(__N_SSP)라 context.request.get으로 HTML만 받아도
  <script id="__NEXT_DATA__">의 props.pageProps.data.map에 주문 정보가 통째로
  들어있다(0.15초, 28KB). ordItemList[]마다 lastOrdStatGbcdNm(상품준비/배송완료…),
  invoice(Y/N - 송장 유무), ordPtcSeq(상품 순번), uitmTotNm(옵션), shipPrrgDtm
  (출고예정일), oshpDtm(출고일시)가 있어 미발급/취소는 여기서 끝난다.
- 송장은 "배송조회" 클릭이 부르는 GET api.hmall.com/api/hf/od/v1/mypage/shpg-inf/
  dlv-trcurl?ordNo=&ordPtcSeq= 의 respData(invcNo 송장, dlvcoNm 택배사명)로
  받는다(0.04초). 브라우저는 accessToken 쿠키를 Authorization: Bearer로 붙이는데,
  실측으로는 쿠키·헤더 없이도 200이 나온다 - 그래도 브라우저와 같게 붙여 보낸다.
  같은 계정의 주문목록 API(shpg-inf/list)는 이 토큰으로도 401(feign-auth)이라
  목록 선읽기는 하지 않는다 - 상세 요청이 0.15초라 필요도 없다.
- 로그인 판정은 상세 요청의 307 Location(/mo/cob/loginForm)으로 본다. accessToken은
  10분짜리 JWT이고 refreshToken으로 조용히 재발급하는 경로가 없다(next-auth
  session·csrf, login/refresh 류 전부 확인) - 화면 방식도 토큰이 없으면 바로
  로그인 폼으로 가므로, 만료 시엔 지금처럼 _auto_login(진짜 크롬 창)을 한 번
  거치고 이어서 요청 방식으로 간다.
- 요청 방식이 판정하지 못하는 응답(HTML 구조가 다름, 송장 API 비정상, 송장 없는
  주문의 상태값이 모르는 값)이면 예전 화면 방식(_get_tracking_via_page)으로
  물러난다. 화면에는 예정 문구가 없어 결과의 '출고/도착예정'이 늘 빈칸이었는데,
  JSON의 shipPrrgDtm을 '출고예정 YYYY-MM-DD'로 실어준다.
- 주문일은 주문번호 앞 8자리(=주문일, ordDtm과 대조 확인). ordDtm은 UTC라
  자정 근처에 하루 어긋날 수 있어 앞 8자리를 못 읽을 때만 +9시간으로 쓴다.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
    raise_if_cancelled_any,
    raise_if_delayed_any,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#userid"
LOGIN_PW_SELECTOR = "#password"
# 제출 버튼은 접근성 이름이 '로그인' 뒤에 '최근 로그인' 배지 텍스트까지 붙어
# 나올 때가 있어(기기에 최근 로그인 기록이 있으면 배지가 생긴다) 이름
# 완전일치로는 못 찾는다 - 폼 안의 버튼을 텍스트 포함으로 찾는다.
LOGIN_BUTTON_SELECTOR = "form[name='login'] button"
LOGIN_PAGE_URL = "https://www.hmall.com/mo/cob/loginForm"
HOME_URL = "https://www.hmall.com/"

# 로그인 제출 시 사이트가 부르는 reCAPTCHA 점수 평가 API.
RECAPTCHA_VERIFY_MARKER = "recaptcha-siteverify"
# 이 점수 밑이면 사이트가 로그인을 거부한다(0.4는 거부, 0.8은 통과 확인).
RECAPTCHA_MIN_SCORE = 0.5

DOMAINS = {"hmall.com", "www.hmall.com"}
SITE_KEY = "hmall"

# 2026-09-09부터 주문당 가벼운 요청 두 번(상세 HTML + 송장 JSON)으로 끝나는
# 사이트. 화면을 열던 때는 (1.0, 2.0)이었고, 요청 방식으로 바뀐 다른 사이트
# (지마켓·4910·신세계TV쇼핑)와 같은 간격으로 둔다. 화면 폴백으로 갔을 때도 이
# 간격이 적용되지만 그 경로는 예외적이라 따로 두지 않는다.
REQUEST_GAP = (0.5, 1.2)


DEFAULT_COURIER = "택배"  # 이동한 URL에서 택배사명을 못 읽었을 때만 쓰는 기본값

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 사람이 직접 로그인할 때 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 제출 후 결과 대기 최대 30초
TRACKING_NAV_WAIT_TIMEOUT_MS = 5 * 1000  # 배송조회 클릭 후 페이지 이동 대기 최대 5초

TRACKING_LINK_TEXT = "배송조회"
TRACKING_URL_MARKER = "selectDlvTrcUrl"

# 요청 방식(2026-09-09) - 파일 맨 위 docstring 참고.
DETAIL_URL = "https://www.hmall.com/mo/mpa/selectOrdPTCPup?ordNo={order_no}"
TRACKING_API_URL = "https://api.hmall.com/api/hf/od/v1/mypage/shpg-inf/dlv-trcurl"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# 브라우저가 api.hmall.com에 붙이는 헤더 중 쿠키에서 그대로 만들 수 있는 것.
ACCESS_TOKEN_COOKIE = "accessToken"
DEVICE_ID_COOKIE = "uh2oxid"
# 송장 유무를 알려주는 상품 칸. "Y"면 배송조회 링크가 뜨는 상품이다.
INVOICE_FLAG_KEY = "invoice"
STATUS_KEY = "lastOrdStatGbcdNm"
KST = timezone(timedelta(hours=9))
# "상품준비"는 2026-09-09 실측값(장은진 20260908067895) - 현대몰은 "중"을 안 붙인다.
NOT_YET_PATTERNS = ["결제완료", "상품준비", "배송준비중", "주문접수"]


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(page, lambda url: "login" in url.lower())


def _prefill_login_id(page: Page) -> None:
    """자동 로그인이 안 될 때(HMALL_PW 없음/점수 미달) 쓰는 폴백 - 아이디만 채운다.

    이 경로에서는 로그인 버튼을 자동으로 누르지 않는다 - 사람이 직접 누르는
    편이 reCAPTCHA 점수에 유리하고, 어차피 사람이 비밀번호를 치는 중이다.
    """
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("HMALL_ID"))


def _warm_up(page: Page) -> None:
    """로그인 전에 홈에서 잠깐 돌아다녀 프로필에 이력을 만든다.

    reCAPTCHA v3는 페이지에 머문 시간과 상호작용도 점수에 반영한다 - 로그인
    페이지로 바로 직행했을 때보다 이 워밍업을 거쳤을 때 통과했다.
    """
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(2000)


def _auto_login(context: BrowserContext) -> bool:
    """HMALL_ID/HMALL_PW로 자동 로그인하고, 받은 쿠키를 원래 컨텍스트에 옮긴다.

    로그인은 진짜 크롬 창(browser.real_chrome_context)에서만 통과한다 - 이유와
    실측 점수는 이 파일 맨 위 docstring 참고. 조회까지 그 창에서 하지는 않고,
    GSSHOP과 마찬가지로 쿠키만 원래 컨텍스트로 옮긴다.

    비밀번호가 없거나 점수가 낮으면 False를 돌려주고 호출자가 기존 수동
    로그인 경로로 넘어간다.
    """
    login_id = os.environ.get("HMALL_ID")
    login_pw = os.environ.get("HMALL_PW")
    if not login_id or not login_pw:
        return False

    try:
        login_context = browser_mod.real_chrome_context(SITE_KEY)
    except Exception as exc:  # 크롬 미설치 등 - 수동 경로로 넘긴다
        common.safe_print(f"[hmall] 자동 로그인용 크롬을 띄우지 못했습니다({exc}) - 직접 로그인으로 넘어갑니다.")
        return False

    scores: list[float] = []

    def _on_response(response) -> None:
        if RECAPTCHA_VERIFY_MARKER not in response.url:
            return
        try:
            data = (response.json() or {}).get("respData") or {}
            scores.append(float(data.get("score")))
        except Exception:
            return

    try:
        page = login_context.pages[0] if login_context.pages else login_context.new_page()
        page.on("response", _on_response)

        _warm_up(page)
        page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        if not _looks_like_login_page(page):
            # 이 프로필에 로그인이 남아 있으면 쿠키만 옮기고 끝낸다.
            context.add_cookies(login_context.cookies())
            return True

        if page.locator(LOGIN_ID_SELECTOR).count() == 0:
            common.safe_print("[hmall] 로그인 페이지에서 아이디 입력창을 찾지 못했습니다 - 직접 로그인으로 넘어갑니다.")
            return False

        page.locator(LOGIN_ID_SELECTOR).click()
        page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=80)
        page.wait_for_timeout(300)
        page.locator(LOGIN_PW_SELECTOR).click()
        page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=80)
        page.wait_for_timeout(600)

        button = page.locator(LOGIN_BUTTON_SELECTOR, has_text="로그인")
        if button.count() == 0:
            common.safe_print("[hmall] 로그인 버튼을 찾지 못했습니다 - 직접 로그인으로 넘어갑니다.")
            return False
        button.first.click()

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            if not _looks_like_login_page(page):
                context.add_cookies(login_context.cookies())
                common.safe_print("[hmall] 자동 로그인에 성공했습니다.")
                return True
            if scores and scores[-1] < RECAPTCHA_MIN_SCORE:
                # 점수가 낮으면 사이트가 자격증명과 무관하게 거부한다 - 더 기다릴 이유가 없다.
                common.safe_print(
                    f"[hmall] reCAPTCHA 점수가 낮아(={scores[-1]}) 사이트가 로그인을 거부했습니다 "
                    "- 직접 로그인으로 넘어갑니다."
                )
                return False
            elapsed_ms += 1500

        common.safe_print("[hmall] 자동 로그인 결과를 30초 안에 확인하지 못했습니다 - 직접 로그인으로 넘어갑니다.")
        return False
    except Exception as exc:
        common.safe_print(f"[hmall] 자동 로그인 중 오류({exc}) - 직접 로그인으로 넘어갑니다.")
        return False
    finally:
        try:
            login_context.close()
        except Exception:
            pass


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _parse_tracking_url(url: str) -> tuple[str, str]:
    qs = parse_qs(urlparse(url).query)
    wbno_values = qs.get("wbno")
    if not wbno_values:
        raise ParseError(f"배송조회 결과 URL에서 송장번호(wbno)를 찾지 못했습니다: {url}")
    tracking_no = re.sub(r"[^0-9]", "", wbno_values[0])

    codename_values = qs.get("codename")
    courier = common.normalize_courier(codename_values[0].strip()) if codename_values and codename_values[0].strip() else DEFAULT_COURIER

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
        raise_if_cancelled(body_text, order_no)
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


class _NeedsLogin(Exception):
    """요청 방식에서 로그인 폼으로 넘겨졌다 - 로그인하고 다시 시도한다."""


class _RequestPathUnusable(Exception):
    """요청 방식으로는 결론을 못 낸다 - 화면 방식으로 물러난다.

    미발급/취소/성공 같은 진짜 결론은 이 예외가 아니라 어댑터 예외로 곧장
    나간다. 이 예외는 '응답 모양이 예상과 다르다'는 뜻일 때만 쓴다.
    """


def _fetch_detail(context: BrowserContext, order_no: str) -> dict:
    """주문상세 HTML만 받아 __NEXT_DATA__의 주문 정보(data.map)를 돌려준다."""
    url = DETAIL_URL.format(order_no=order_no)
    response = context.request.get(url, max_redirects=0)
    if 300 <= response.status < 400:
        location = response.headers.get("location", "")
        if "login" in location.lower():
            raise _NeedsLogin()
        response = context.request.get(url)  # 로그인이 아닌 다른 리다이렉트면 따라간다
        if "login" in response.url.lower():
            raise _NeedsLogin()
    if response.status != 200:
        raise _RequestPathUnusable(f"주문상세 응답이 HTTP {response.status}")
    match = NEXT_DATA_RE.search(response.text())
    if not match:
        raise _RequestPathUnusable("주문상세 HTML에 __NEXT_DATA__가 없음")
    try:
        data = json.loads(match.group(1))
        detail = data["props"]["pageProps"]["data"]["map"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _RequestPathUnusable(f"__NEXT_DATA__ 구조가 다름 ({exc!r})") from exc
    items = detail.get("ordItemList") if isinstance(detail, dict) else None
    if not items:
        raise _RequestPathUnusable("ordItemList가 비어 있음")
    if str(detail.get("ordNo") or "") != order_no:
        raise _RequestPathUnusable(f"응답의 주문번호가 다름 ({detail.get('ordNo')})")
    return detail


def _order_date_of(order_no: str, detail: dict) -> date | None:
    """주문번호 앞 8자리가 주문일이다. 못 읽으면 ordDtm(UTC)을 한국 시간으로."""
    try:
        return datetime.strptime(order_no[:8], "%Y%m%d").date()
    except ValueError:
        pass
    raw = str(detail.get("ordDtm") or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(KST)
    return parsed.date()


def _delivery_note(items: list[dict]) -> str | None:
    """출고예정일(shipPrrgDtm "2026-09-11 00:00:00")을 '출고예정 2026-09-11'로."""
    notes: list[str] = []
    for item in items:
        raw = str(item.get("shipPrrgDtm") or "")[:10]
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
            note = f"출고예정 {raw}"
            if note not in notes:
                notes.append(note)
    return " / ".join(notes) if notes else None


def _select_items(items: list[dict], order_option: str | None) -> list[dict]:
    """샵마인 "주문옵션"이 상품명+옵션(uitmTotNm)에 유일하게 맞는 상품만 남긴다.

    화면 방식의 _select_link_index_by_order_option과 같은 규칙 - 0개 또는 2개
    이상 맞으면 전부 돌려주고, 호출자가 송장을 비교한다.
    """
    if len(items) <= 1 or not order_option:
        return items
    target = normalize_option(order_option)
    if not target:
        return items
    matched = [
        item for item in items
        if target in normalize_option(f"{item.get('ordItemNm') or ''} {item.get('uitmTotNm') or ''}")
    ]
    return matched if len(matched) == 1 else items


def _api_headers(context: BrowserContext) -> dict[str, str]:
    headers = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.hmall.com",
               "Referer": "https://www.hmall.com/", "device": "mobile"}
    for cookie in context.cookies("https://www.hmall.com/"):
        if cookie["name"] == ACCESS_TOKEN_COOKIE and cookie["value"]:
            headers["Authorization"] = f"Bearer {cookie['value']}"
        elif cookie["name"] == DEVICE_ID_COOKIE and cookie["value"]:
            headers[DEVICE_ID_COOKIE] = cookie["value"]
    return headers


def _fetch_tracking(context: BrowserContext, order_no: str, ptc_seq) -> tuple[str, str]:
    """배송조회 클릭이 부르는 송장 API - (송장번호, 택배사명)."""
    response = context.request.get(
        TRACKING_API_URL, params={"ordNo": order_no, "ordPtcSeq": ptc_seq}, headers=_api_headers(context))
    if response.status != 200:
        raise _RequestPathUnusable(f"송장 API가 HTTP {response.status}")
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - JSON이 아니면 화면 방식으로
        raise _RequestPathUnusable("송장 API 응답이 JSON이 아님") from exc
    data = body.get("respData") if isinstance(body, dict) else None
    if body.get("successYn") != "Y" or not isinstance(data, dict):
        raise _RequestPathUnusable(f"송장 API 거부 ({body.get('respCode')}: {body.get('respMsg')})")
    tracking_no = re.sub(r"[^0-9]", "", str(data.get("invcNo") or ""))
    if not tracking_no:
        raise _RequestPathUnusable("송장 API 응답에 invcNo가 없음")
    name = str(data.get("dlvcoNm") or "").strip()
    courier = common.normalize_courier(name) if name else DEFAULT_COURIER
    return tracking_no, courier


def _resolve_from_json(context: BrowserContext, order_no: str, items: list[dict],
                       order_option: str | None) -> TrackingResult:
    chosen = _select_items(items, order_option)
    shipped = [item for item in chosen if str(item.get(INVOICE_FLAG_KEY) or "").upper() == "Y"]
    if not shipped:
        statuses = [str(item.get(STATUS_KEY) or "") for item in chosen]
        # 상태값을 정확히 아는 공급사 규칙 - 취소/지연을 먼저, 미발급은 그 다음.
        raise_if_cancelled_any(statuses, order_no)
        raise_if_delayed_any(statuses, order_no)
        joined = "/".join(statuses)
        if any(p in status for status in statuses for p in NOT_YET_PATTERNS):
            raise TrackingNotAvailableYet(
                f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={joined}).")
        raise _RequestPathUnusable(f"송장이 없는데 상태를 모름 (상태={joined})")

    results = [_fetch_tracking(context, order_no, item.get("ordPtcSeq")) for item in shipped]
    if len({tracking_no for tracking_no, _ in results}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) "
            "- 상품별로 나눠 배송된 것으로 보입니다.")
    tracking_no, courier = results[0]
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def _lookup_via_request(context: BrowserContext, order_no: str, order_option: str | None) -> TrackingResult:
    detail = _fetch_detail(context, order_no)
    items = detail["ordItemList"]
    return attach_order_date(
        _order_date_of(order_no, detail),
        lambda: _resolve_from_json(context, order_no, items, order_option),
        delivery_note=_delivery_note(items),
    )


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    """요청 방식으로 먼저, 결론을 못 내면 화면 방식으로 (파일 맨 위 docstring)."""
    order_no = extract_order_no(product_url)
    try:
        return _lookup_via_request(context, order_no, order_option)
    except _NeedsLogin:
        # 자동 로그인은 자체 크롬 창을 띄우므로 headless 실행 중에도 쓸 수 있다.
        # 실패하면(비밀번호 없음/점수 미달) 화면 방식이 수동 로그인까지 맡는다.
        if _auto_login(context):
            try:
                return _lookup_via_request(context, order_no, order_option)
            except _NeedsLogin:
                raise BlockedError("자동 로그인 후에도 여전히 로그인 페이지입니다.") from None
            except _RequestPathUnusable as exc:
                common.safe_print(f"[hmall] 요청 방식으로 판정하지 못했습니다({exc}) - 화면 방식으로 조회합니다.")
    except _RequestPathUnusable as exc:
        common.safe_print(f"[hmall] 요청 방식으로 판정하지 못했습니다({exc}) - 화면 방식으로 조회합니다.")
    return _get_tracking_via_page(context, product_url, headless, order_option)


def _get_tracking_via_page(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    """2026-09-09 이전의 조회 - 주문상세 화면을 열고 "배송조회"를 클릭한다."""
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            # 자동 로그인은 자체 크롬 창을 띄우므로 headless 실행 중에도 쓸 수 있다.
            if _auto_login(context):
                page.goto(product_url, wait_until="domcontentloaded")
                if _looks_like_login_page(page):
                    raise BlockedError("자동 로그인 후에도 여전히 로그인 페이지입니다.")
            elif headless:
                raise BlockedError(
                    "현대몰 로그인이 필요합니다. HMALL_PW를 넣거나, --headless 없이 실행해 수동으로 로그인해주세요."
                )
            else:
                _prefill_login_id(page)
                common.safe_print("[hmall] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[hmall] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
                page.goto(product_url, wait_until="domcontentloaded")
                if _looks_like_login_page(page):
                    raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        # 주문상세는 자바스크립트로 그려진다 - 주문번호가 화면에 뜨면 다 그려진 것이다.
        common.wait_for_text(page, order_no)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, product_url, order_no, order_option))
    finally:
        page.close()
