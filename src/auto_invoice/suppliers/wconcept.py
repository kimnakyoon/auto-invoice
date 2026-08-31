"""W컨셉(www.wconcept.co.kr) 공급사 어댑터.

리버스엔지니어링 결과(2026-08-31 실측):
- 주문상세 URL: https://www.wconcept.co.kr/MyPage/MyOrderDetailView?orderno=<주문번호>
  (샵마인 엑셀의 "상품URL"에 이 형태로 들어온다. 주문번호는 Z13335855처럼
  영문 한 글자 + 숫자다.)
- 로그인이 안 되어 있으면 /Member/Login?rUrl=... 으로 리다이렉트된다. 폼은
  form#frmLogin, 아이디 input#custId(이메일), 비밀번호 input#custPw,
  제출은 그 폼 안의 button[type=submit]이고 POST /Member/LoginProcess로 간다.
- **로그인 폼에 reCAPTCHA Enterprise가 걸려 있다.** 다른 사이트처럼 번들
  Chromium(headless)에서 아이디/비밀번호를 채우고 누르면 점수 미달로 거부되고,
  그냥 실패로 끝나는 게 아니라 사이트가 **30분 간 로그인을 막는다**
  ("비정상적인 로그인 시도로 30분 간 로그인이 제한됩니다" - 2026-08-31 실측).
  그래서 현대몰(hmall)과 같은 구조로, **로그인만 browser.real_chrome_context()로
  띄운 진짜 크롬 창**에서 하고 성공하면 쿠키만 원래 headless 컨텍스트로 옮긴다
  (조회는 지금까지처럼 창 없이 진행된다). 사람이 타이핑하거나 체크박스를 누를
  일은 없다.
- 로그인 실패 사유는 폼 안의 p.incorrect 중 **보이는 것**에 나온다(아이디/
  비밀번호 불일치, 30분 제한 등). 이 문구를 그대로 실어 BlockedError를 던진다 -
  30분 제한에 걸린 상태에서 계속 두드리면 제한만 길어지므로, 오케스트레이터가
  그 사이트의 남은 주문을 전부 건너뛰게 해서 한 번 실행에 로그인 시도는
  최대 1회가 되게 한다.
- 로그인 성공 후 돌아가는 곳은 원래 주문상세가 아니다 - 리다이렉트 파라미터
  (rUrl/returnURL)에 orderno가 빠진 채 /MyPage/MyOrderDetailView만 담겨서
  주문번호 없는 화면(주문목록)으로 떨어진다. 그래서 로그인 뒤에는 항상
  주문상세 URL로 다시 들어간다.
- 주문상세의 상품 목록은 table.tbl_order_list의 tr 하나가 상품 하나다.
    옵션      p.option              ("옵션 : COLOR 진블루 WBWTB5M80T,SIZE L")
    진행상황  span[name=statusname] ("배송중")
    배송조회  button[onclick*='trace.goodsflow.com']
  (같은 줄의 td.delivery는 택배사가 아니라 **배송비**다 - "무료"가 들어온다.)
- 배송조회 버튼은 goodsflow 배송추적 창을 새로 연다:
    window.open('https://trace.goodsflow.com/VIEW/V1/whereis/wconcept/0627214...')
  그 창이 그리는 화면에는 송장번호가 없다 - 집화 전이면 "배송조회 상세내역이
  없습니다"만 나온다. 대신 그 화면이 부르는
    POST https://trace.goodsflow.com/VIEW/api/tracking
    {"memberCode":"wconcept","uniqueCode":"<경로 마지막 조각>"}
  응답에 baseData.invoiceNo(송장번호)와 baseData.logisticsName(택배사명,
  실측값 "CJ대한통운")이 집화 전에도 들어 있다. 이 API는 **쿠키도 로그인도
  없이** 응답하는 것을 확인했다(로그인하지 않은 브라우저로 실측). 그래서
  창을 열지 않고 URL에서 두 코드만 뽑아 API를 직접 부른다.
- 상품이 여러 개라 배송조회 버튼이 여러 개인 주문은, 샵마인 엑셀의 "주문옵션"
  으로 어느 줄인지 특정되면 그 줄만 본다. 특정할 수 없으면 다른 어댑터와 같은
  안전 규칙으로, 송장번호가 전부 같을 때만 쓰고 다르면 사람이 확인하도록
  예외를 던진다.
- 주문일은 화면의 "주문번호 Z13335855 주문일 2026.08.30"에서 읽힌다
  (order_date.py의 라벨 규칙).
"""

from __future__ import annotations

import os
import re
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
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

DOMAINS = {"wconcept.co.kr", "www.wconcept.co.kr"}
SITE_KEY = "wconcept"

HOME_URL = "https://www.wconcept.co.kr/"
LOGIN_PATH = "/member/login"
LOGIN_URL = "https://www.wconcept.co.kr/Member/Login"
LOGIN_ID_SELECTOR = "#custId"
LOGIN_PW_SELECTOR = "#custPw"
LOGIN_BUTTON_SELECTOR = "#frmLogin button[type='submit']"
# 로그인 실패 사유가 적힌 문구들(숨어 있다가 해당하는 것만 보이게 된다).
LOGIN_ERROR_SELECTOR = "#frmLogin .incorrect"

LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 제출 후 결과 대기
GOODS_TABLE_TIMEOUT_MS = 15 * 1000  # 주문상세의 상품 목록이 그려질 때까지

GOODS_TABLE_SELECTOR = "table.tbl_order_list"

DEFAULT_COURIER = "택배"  # 배송추적 응답에 택배사명이 없을 때만 쓴다

# 배송조회 버튼은 goodsflow 배송추적 창을 연다:
#   onclick="window.open('https://trace.goodsflow.com/VIEW/V1/whereis/wconcept/0627...')"
# 경로의 마지막 두 조각이 그 창이 API에 넘기는 memberCode와 uniqueCode다.
TRACE_URL_PATTERN = re.compile(
    r"trace\.goodsflow\.com/VIEW/V1/whereis/([^/'\"?]+)/([^/'\"?]+)")
TRACKING_API_URL = "https://trace.goodsflow.com/VIEW/api/tracking"

# 아직 발송 전이라 송장이 없는 것이 정상인 진행상황(span[name=statusname]).
# 실측한 값은 "배송중"뿐이라, 나머지는 다른 어댑터와 같은 표기로 넣어뒀다.
NOT_YET_STATUSES = ["결제완료", "입금대기", "주문접수", "상품준비중", "배송준비중", "배송대기"]

# 상품 한 줄에서 필요한 것만 뽑아온다 (옵션 / 진행상황 / 배송조회 onclick).
ROWS_JS = """() => Array.from(document.querySelectorAll('table.tbl_order_list tbody tr')).map(tr => {
  const pick = (sel) => { const el = tr.querySelector(sel); return el ? el.innerText.trim() : ''; };
  const btn = tr.querySelector("button[onclick*='trace.goodsflow.com']");
  return {
    option: pick('p.option'),
    name: pick('p.product_name'),
    status: pick("[name='statusname']"),
    onclick: btn ? (btn.getAttribute('onclick') || '') : '',
  };
})"""


def extract_order_no(product_url: str) -> str:
    values = parse_qs(urlparse(product_url).query).get("orderno")
    if not values or not values[0].strip():
        raise ParseError(f"URL에서 orderno 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0].strip()


def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(
        page, lambda url: urlparse(url).path.rstrip("/").lower() == LOGIN_PATH)


def _login_error(page: Page) -> str | None:
    """지금 화면에 보이는 로그인 실패 문구. 없으면 None."""
    try:
        messages = page.eval_on_selector_all(
            LOGIN_ERROR_SELECTOR,
            "els => els.filter(e => e.offsetParent !== null).map(e => e.innerText.trim())",
        )
    except Exception:  # noqa: BLE001 - 문구를 못 읽어도 로그인 판정 자체는 계속한다
        return None
    return " / ".join(m for m in messages if m) or None


def _warm_up(page: Page) -> None:
    """로그인 전에 홈에서 잠깐 돌아다녀 프로필에 이력을 만든다.

    reCAPTCHA는 페이지에 머문 시간과 상호작용도 점수에 반영한다(현대몰에서
    같은 워밍업으로 0.4 -> 0.8이 됐다).
    """
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(2000)


def _auto_login(context: BrowserContext) -> None:
    """WCONCEPT_ID/WCONCEPT_PW로 자동 로그인하고 쿠키를 원래 컨텍스트에 옮긴다.

    로그인은 진짜 크롬 창에서만 통과한다 - 이유는 이 파일 맨 위 docstring 참고.
    실패하면 사이트가 준 문구를 그대로 실어 BlockedError를 던진다(30분 제한에
    걸린 채로 다시 두드리지 않게 하려는 것이다).
    """
    login_id = os.environ.get("WCONCEPT_ID")
    login_pw = os.environ.get("WCONCEPT_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "W컨셉 로그인이 필요하지만 WCONCEPT_ID/WCONCEPT_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    try:
        login_context = browser_mod.real_chrome_context(
            SITE_KEY, viewport=browser_mod.DESKTOP_VIEWPORT)
    except Exception as exc:  # noqa: BLE001 - 크롬 미설치 등
        raise BlockedError(
            f"W컨셉 로그인용 크롬 창을 띄우지 못했습니다({exc}) - 이 사이트는 봇 확인 때문에 "
            "설치된 진짜 크롬으로만 로그인할 수 있습니다."
        ) from exc

    try:
        page = login_context.pages[0] if login_context.pages else login_context.new_page()

        _warm_up(page)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        if not _looks_like_login_page(page):
            # 이 크롬 프로필에 로그인이 남아 있으면 쿠키만 옮기고 끝낸다.
            context.add_cookies(login_context.cookies())
            return

        page.locator(LOGIN_ID_SELECTOR).click()
        page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=80)
        page.wait_for_timeout(300)
        page.locator(LOGIN_PW_SELECTOR).click()
        page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=80)
        page.wait_for_timeout(600)
        page.locator(LOGIN_BUTTON_SELECTOR).first.click()

        elapsed_ms = 0
        while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
            page.wait_for_timeout(1000)
            if not _looks_like_login_page(page):
                context.add_cookies(login_context.cookies())
                common.safe_print("[wconcept] 자동 로그인에 성공했습니다.")
                return
            message = _login_error(page)
            if message:
                # 사이트가 사유를 알려준 이상 더 기다릴 이유가 없다.
                raise BlockedError(f"W컨셉 로그인 실패 - {message}")
            elapsed_ms += 1000

        raise BlockedError("W컨셉 자동 로그인 결과를 30초 안에 확인하지 못했습니다.")
    finally:
        try:
            login_context.close()
        except Exception:  # noqa: BLE001 - 창을 못 닫아도 결과에 영향은 없다
            pass


def _tracking_from_goodsflow(context: BrowserContext, member_code: str,
                             unique_code: str, order_no: str) -> tuple[str, str]:
    """goodsflow 배송추적 API에서 (송장번호, 택배사)를 읽는다.

    쿠키도 로그인도 필요 없이 memberCode+uniqueCode만으로 응답한다(로그인하지
    않은 브라우저로 확인). 그래서 배송조회 창을 실제로 열지 않고 이 API만
    부른다 - 창을 띄우면 그 화면은 '집화 전'일 때 "상세내역이 없습니다"만
    보여주는데, 그때도 API 응답에는 송장번호와 택배사가 들어 있다.
    """
    response = context.request.post(
        TRACKING_API_URL, data={"memberCode": member_code, "uniqueCode": unique_code}
    )
    if not response.ok:
        raise ParseError(
            f"배송추적 API가 응답하지 않았습니다 (주문번호={order_no}, HTTP {response.status})."
        )
    body = response.json() or {}
    base = body.get("baseData") or {}

    invoice_no = re.sub(r"[^0-9]", "", str(base.get("invoiceNo") or ""))
    if not invoice_no:
        raise ParseError(
            f"배송추적 응답에 송장번호가 없습니다 (주문번호={order_no}, "
            f"사유={body.get('errorMessage') or base.get('errorMsg') or '알 수 없음'})."
        )

    name = str(base.get("logisticsName") or "").strip()
    return invoice_no, (common.normalize_courier(name) if name else DEFAULT_COURIER)


def _trace_codes(onclick: str) -> tuple[str, str] | None:
    """배송조회 버튼의 onclick에서 (memberCode, uniqueCode)를 뽑는다. 없으면 None."""
    found = TRACE_URL_PATTERN.search(onclick or "")
    return (found[1], found[2]) if found else None


def _clean_option(text: str) -> str:
    """화면의 "옵션 : COLOR 진블루,SIZE L"에서 앞의 라벨을 떼어낸다."""
    return re.sub(r"^\s*옵션\s*[:：]?\s*", "", text or "")


def _select_row_by_order_option(rows: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션"과 같은 옵션이 적힌 줄이 딱 하나면 그 줄.

    표기가 사이트마다 조금씩 달라서(", " vs " / ") normalize_option으로
    공백·구분자를 지우고 비교하고, 한쪽이 다른 쪽을 포함하는 경우도 같은
    것으로 본다. 후보가 0개거나 2개 이상이면 None - 호출한 쪽이 송장번호를
    서로 비교하는 안전 규칙으로 넘어간다.
    """
    target = normalize_option(order_option)
    if not target or len(rows) <= 1:
        return None
    candidates = []
    for row in rows:
        value = normalize_option(_clean_option(row.get("option", "")))
        if value and (value == target or value in target or target in value):
            candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


def _raise_not_shipped(rows: list[dict], order_no: str) -> None:
    """배송조회 버튼이 없는 줄들의 진행상황을 보고 '아직 미발급'인지 '취소'인지 가른다.

    진행상황(span[name=statusname])만 따로 읽을 수 있으므로 취소/품절 판정은
    그 값으로만 한다 - 화면 전체 텍스트에는 '취소/반품 조회' 같은 메뉴 글자가
    늘 있어서, 그걸로 판정하면 멀쩡한 주문이 전부 취소로 둔갑한다
    (base.py 주석의 '주문상태를 정확히 읽을 수 있는 공급사' 규칙).
    """
    statuses = [row.get("status", "").strip() for row in rows if row.get("status", "").strip()]
    for status in statuses:
        raise_if_cancelled(status, order_no)
    normalized = [normalize_option(s) for s in statuses]
    if any(normalize_option(p) in n for p in NOT_YET_STATUSES for n in normalized):
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
    raise ParseError(
        f"화면에서 배송조회 버튼을 찾지 못했습니다 (주문번호={order_no}, "
        f"진행상황={' / '.join(statuses) or '읽지 못함'})."
    )


def _scrape_tracking_from_page(page: Page, context: BrowserContext, order_no: str,
                               order_option: str | None) -> TrackingResult:
    rows = page.evaluate(ROWS_JS)
    if not rows:
        raise ParseError(f"주문상세에서 상품 목록을 찾지 못했습니다 (주문번호={order_no}).")

    # 옵션으로 어느 상품인지 확정되면 그 줄만 본다 - 다른 상품이 이미 나갔더라도
    # 우리가 올려야 하는 상품의 송장이 아니면 안 된다.
    matched = _select_row_by_order_option(rows, order_option)
    if matched is not None:
        rows = [matched]

    shipped = [(row, codes) for row in rows if (codes := _trace_codes(row.get("onclick", "")))]
    if not shipped:
        _raise_not_shipped(rows, order_no)

    # 배송조회 주소가 전부 같으면 API를 한 번만 부른다(같은 송장이 확정이다).
    distinct_codes = {codes for _, codes in shipped}
    results = {
        _tracking_from_goodsflow(context, member_code, unique_code, order_no)
        for member_code, unique_code in distinct_codes
    }
    if len(results) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "상품별로 나눠 배송된 것으로 보입니다."
        )

    tracking_no, courier = next(iter(results))
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            common.safe_print("[wconcept] 로그인 세션이 없어 자동 로그인을 시도합니다.")
            _auto_login(context)
            # 로그인 후에는 주문상세로 돌아오지 않는다(리다이렉트 값에 orderno가
            # 빠져 있다) - 항상 원래 주소로 다시 들어간다.
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("W컨셉 로그인 후에도 여전히 로그인 페이지입니다.")

        page.wait_for_selector(GOODS_TABLE_SELECTOR, timeout=GOODS_TABLE_TIMEOUT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(
            page, lambda: _scrape_tracking_from_page(page, context, order_no, order_option))
    finally:
        page.close()
