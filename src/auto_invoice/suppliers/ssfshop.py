"""SSF샵(www.ssfshop.com / 삼성물산 패션부문) 공급사 어댑터.

리버스엔지니어링 결과(2026-08-31 실측):
- 주문상세 URL: https://www.ssfshop.com/secured/mypage/<주문번호>/orderInfo
  (샵마인 엑셀의 "상품URL"에 이 형태로 들어온다. 주문번호는 OD로 시작하는
  경로 조각이라 쿼리 파라미터가 아니라 경로에서 뽑는다.)
- 로그인이 안 되어 있으면 https://www.ssfshop.com/public/member/login 으로
  리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#userId, 비밀번호
  input#password, 버튼은 form#loginForm 안의 onclick="login();" 버튼.
  비밀번호를 자바스크립트로 암호화하지 않고 폼을 그대로 POST /loginProcess로
  보내며(캡차·봇 확인 없음), 로그인에 성공하면 원래 보려던 주문상세로 바로
  돌아온다. 그래서 SSG/더현대와 같은 방식으로 SSFSHOP_ID/SSFSHOP_PW로 완전
  자동 로그인한다. 세션은 storage_state(쿠키)로 저장되므로 최초 1회만 자동
  로그인하고 이후 실행은 쿠키로 바로 조회한다.
- **비밀번호가 틀리면 5회에서 계정이 잠긴다.** 응답 JSON의 failMessage에
  "아이디/비밀번호가 일치하지 않습니다.<br>[3회 / 총5회]" 처럼 남은 횟수가
  같이 온다. 그래서 실패를 기다리지 않고 그 자리에서 BlockedError로 그 문구를
  그대로 올린다 - 오케스트레이터가 그 사이트의 남은 주문을 전부 건너뛰므로
  한 번 실행에 로그인 시도는 최대 1회다(잠김 방지).
- 주문상세의 상품 목록은 table.tbl-goods의 tr 하나가 상품 하나다. 한 줄에
    옵션      dd.option        ("검정색, S(90)")
    주문상태  .stat-td dt      ("배송완료" / "상품 준비중")
    배송조회  [onclick*='checkDelivery']
  가 들어 있다. **송장번호와 택배사가 배송조회 버튼의 onclick 인자에 그대로
  박혀 있어서** 버튼을 누르거나 API를 가로챌 필요가 없다:
    checkDelivery('49258780314', 'https://www.goodsflow.io/tracking/...',
                  'LOGEN','DLV_COMPT','N','PARTMAL', '로젠택배')
  순서대로 송장번호, 배송추적 URL, 택배사 코드, 배송상태 코드, ?, 판매유형,
  택배사 이름이다. 실측한 값은 LOGEN/로젠택배, LOTTE/롯데택배,
  CJKEX/CJ대한통운.
- 주문상태(.stat-td dt)만 따로 읽을 수 있으므로 취소/품절 판정은 그 값으로만
  한다. 같은 칸의 설명문에 "직접 취소가 불가합니다" 같은 문구가 있어서
  화면 전체 텍스트로 판정하면 멀쩡한 주문이 취소로 둔갑한다(base.py 주석의
  '주문상태를 정확히 읽을 수 있는 공급사' 규칙).
- 주문일은 화면의 "결제일시 2026.08.30 21:31:22"에서 읽힌다(order_date.py의
  라벨 규칙). 주문번호 옆에도 날짜가 있지만 라벨이 없어 그쪽은 쓰지 않는다.
- 상품이 여러 개라 배송조회 버튼이 여러 개인 주문은, 샵마인 엑셀의 "주문옵션"
  으로 어느 줄인지 특정되면 그 줄만 본다. 특정할 수 없으면 다른 어댑터와 같은
  안전 규칙으로, 송장번호가 전부 같을 때만 쓰고 다르면 사람이 확인하도록
  예외를 던진다.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

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

DOMAINS = {"ssfshop.com", "www.ssfshop.com"}
SITE_KEY = "ssfshop"

LOGIN_PATH = "/public/member/login"
LOGIN_ID_SELECTOR = "#userId"
LOGIN_PW_SELECTOR = "#password"
LOGIN_BUTTON_SELECTOR = "#loginForm button[onclick*='login']"
LOGIN_API_MARKER = "/loginProcess"

GOODS_TABLE_SELECTOR = "table.tbl-goods"

LOGIN_FORM_TIMEOUT_MS = 60 * 1000  # 로그인 폼이 뜰 때까지 (첫 접속이 느릴 때가 있다)
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 로그인 제출 후 리다이렉트 대기
GOODS_TABLE_TIMEOUT_MS = 15 * 1000  # 주문상세의 상품 목록이 그려질 때까지

# 아직 발송 전이라 송장이 없는 것이 정상인 주문상태 (.stat-td dt 값).
# "상품 준비중"은 실제 미발송 주문에서 확인했고, 나머지는 SSF샵 주문 단계
# 안내(결제 완료 -> 상품 준비중 -> 배송중 -> 배송완료)에서 가져왔다.
NOT_YET_STATUSES = ["결제완료", "결제 완료", "입금대기", "주문접수", "상품준비중", "배송준비중"]

# 택배사 이름이 비어 있을 때 코드로 대신 찾는다. 실측한 코드만 적어둔다 -
# 여기 없는 코드는 common.normalize_courier가 이름 규칙으로 처리한다
# (CJ*/롯데*/DELIBOX는 그쪽에서 걸린다).
COURIER_CODE_NAMES = {
    "LOGEN": "로젠택배",
    "LOTTE": "롯데택배",
    "CJKEX": "CJ대한통운",
}

# 주문번호는 경로 조각이다: /secured/mypage/OD202608307796482/orderInfo
ORDER_NO_PATTERN = re.compile(r"(OD\d+)", re.IGNORECASE)

# checkDelivery('송장번호', '추적URL', '택배사코드', '배송상태', 'N', '판매유형', '택배사명')
CHECK_DELIVERY_PATTERN = re.compile(r"checkDelivery\s*\((.*?)\)", re.DOTALL)
QUOTED_ARG_PATTERN = re.compile(r"'([^']*)'")

# 상품 한 줄에서 필요한 것만 뽑아온다 (옵션 / 주문상태 / 배송조회 onclick).
ROWS_JS = """() => Array.from(document.querySelectorAll('table.tbl-goods tbody tr')).map(tr => {
  const pick = (sel) => { const el = tr.querySelector(sel); return el ? el.innerText.trim() : ''; };
  const btn = tr.querySelector("[onclick*='checkDelivery']");
  return {
    option: pick('dd.option'),
    name: pick('.name'),
    status: pick('.stat-td dt'),
    onclick: btn ? (btn.getAttribute('onclick') || '') : '',
  };
})"""


def extract_order_no(product_url: str) -> str:
    found = ORDER_NO_PATTERN.search(urlparse(product_url).path)
    if not found:
        raise ParseError(f"URL에서 주문번호(OD...)를 찾을 수 없습니다: {product_url}")
    return found[1]


def _looks_like_login_page(page: Page) -> bool:
    return common.looks_like_login_page(
        page, lambda url: urlparse(url).path.rstrip("/") == LOGIN_PATH)


def _auto_login(page: Page) -> None:
    """SSFSHOP_ID/SSFSHOP_PW로 완전 자동 로그인한다.

    실패하면 사이트가 준 문구(남은 시도 횟수 포함)를 그대로 실어 BlockedError를
    던진다 - 계정이 잠기기 전에 사람이 비밀번호를 고칠 수 있어야 한다.
    """
    login_id = os.environ.get("SSFSHOP_ID")
    login_pw = os.environ.get("SSFSHOP_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "SSF샵 로그인이 필요하지만 SSFSHOP_ID/SSFSHOP_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    failure: dict[str, str] = {}

    def on_response(response) -> None:
        if LOGIN_API_MARKER not in response.url or response.request.method != "POST":
            return
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 실패 사유를 못 읽어도 로그인 자체는 계속 판정한다
            return
        message = (body or {}).get("failMessage")
        if message:
            failure["message"] = re.sub(r"<[^>]+>", " ", message).strip()

    page.on("response", on_response)
    try:
        page.wait_for_selector(LOGIN_ID_SELECTOR, timeout=LOGIN_FORM_TIMEOUT_MS)
        page.fill(LOGIN_ID_SELECTOR, login_id)
        page.fill(LOGIN_PW_SELECTOR, login_pw)
        page.click(LOGIN_BUTTON_SELECTOR)

        elapsed_ms = 0
        while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            if failure:
                raise BlockedError(f"SSF샵 로그인 실패 - {failure['message']} (.env의 SSFSHOP_PW를 확인해주세요)")
            if not _looks_like_login_page(page):
                return
            elapsed_ms += 1500
    finally:
        page.remove_listener("response", on_response)

    raise BlockedError("SSF샵 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")


def _parse_check_delivery(onclick: str) -> tuple[str, str] | None:
    """배송조회 버튼의 onclick에서 (송장번호, 택배사)를 뽑는다. 없으면 None."""
    found = CHECK_DELIVERY_PATTERN.search(onclick or "")
    if not found:
        return None
    args = QUOTED_ARG_PATTERN.findall(found[1])
    if not args:
        return None

    tracking_no = re.sub(r"[^0-9]", "", args[0])
    if not tracking_no:
        return None

    code = args[2].strip() if len(args) > 2 else ""
    name = args[6].strip() if len(args) > 6 else ""
    raw = name or COURIER_CODE_NAMES.get(code.upper(), code)
    return tracking_no, (common.normalize_courier(raw) if raw else "택배")


def _rows_with_tracking(rows: list[dict]) -> list[tuple[dict, tuple[str, str]]]:
    found = []
    for row in rows:
        tracking = _parse_check_delivery(row.get("onclick", ""))
        if tracking is not None:
            found.append((row, tracking))
    return found


def _select_row_by_order_option(rows: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션"과 같은 옵션이 적힌 줄이 딱 하나면 그 줄.

    표기가 사이트마다 조금씩 달라서(", " vs " / ") normalize_option으로
    공백·구분자를 지우고 비교하고, 한쪽이 다른 쪽을 포함하는 경우도 같은
    것으로 본다. 후보가 0개거나 2개 이상이면 None - 호출한 쪽이 예전처럼
    송장번호를 서로 비교하는 안전 규칙으로 넘어간다.
    """
    target = normalize_option(order_option)
    if not target or len(rows) <= 1:
        return None
    candidates = []
    for row in rows:
        value = normalize_option(row.get("option", ""))
        if value and (value == target or value in target or target in value):
            candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


def _raise_not_shipped(rows: list[dict], order_no: str) -> None:
    """송장이 없는 줄들의 주문상태를 보고 '아직 미발급'인지 '취소/품절'인지 가른다."""
    statuses = [row.get("status", "").strip() for row in rows if row.get("status", "").strip()]
    for status in statuses:
        raise_if_cancelled(status, order_no)
    normalized = [normalize_option(s) for s in statuses]
    if any(normalize_option(p) in n for p in NOT_YET_STATUSES for n in normalized):
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}).")
    raise ParseError(
        f"화면에서 배송조회 정보를 찾지 못했습니다 (주문번호={order_no}, 주문상태={' / '.join(statuses) or '읽지 못함'})."
    )


def _scrape_tracking_from_page(page: Page, order_no: str, order_option: str | None) -> TrackingResult:
    rows = page.evaluate(ROWS_JS)
    if not rows:
        raise ParseError(f"주문상세에서 상품 목록을 찾지 못했습니다 (주문번호={order_no}).")

    # 옵션으로 어느 상품인지 확정되면 그 줄만 본다 - 다른 상품이 이미 나갔더라도
    # 우리가 올려야 하는 상품의 송장이 아니면 안 된다.
    matched = _select_row_by_order_option(rows, order_option)
    if matched is not None:
        rows = [matched]

    shipped = _rows_with_tracking(rows)
    if not shipped:
        _raise_not_shipped(rows, order_no)

    distinct = {tracking for _, tracking in shipped}
    if len(distinct) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - 상품별로 나눠 배송된 것으로 보입니다."
        )

    tracking_no, courier = shipped[0][1]
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            common.safe_print("[ssfshop] 로그인 세션이 없어 자동 로그인을 시도합니다.")
            _auto_login(page)
            # 로그인 성공이면 보통 원래 주문상세로 돌아오지만, 다른 화면으로
            # 떨어지는 경우가 있어 한 번 더 들어간다.
            if not page.url.startswith(product_url):
                page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("SSF샵 로그인 후에도 여전히 로그인 페이지입니다.")

        page.wait_for_selector(GOODS_TABLE_SELECTOR, timeout=GOODS_TABLE_TIMEOUT_MS)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, order_no, order_option))
    finally:
        page.close()
