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
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    OrderCancelled,
    ParseError,
    TrackingNotAvailableYet,
    normalize_option,
    raise_if_cancelled,
    raise_if_cancelled_any,
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

# --------------------------------------------------------------------------
# 주문내역 목록으로 먼저 걸러내기 (prepare_batch)
# --------------------------------------------------------------------------
# 이 도구가 매번 조회하는 주문의 절반 가까이가 롯데온이고(실측 182건 중 74건),
# 그중 대부분은 '아직 안 나갔다'는 답을 듣자고 주문상세를 한 번씩 여는 것이다.
# 마이롯데 > 주문 내역 목록은 한 화면에 15건씩 주문번호·주문상태·주문일을 같이
# 보여주므로, 목록 몇 번이면 상세를 열 필요가 있는 주문만 골라낼 수 있다.
# 2026-08-31 실측: 대기 주문 71건을 [더보기] 12번으로 전부 덮었다.
#
# 목록은 사람이 평소에 보는 화면이라 이 방식은 API 직접 호출과 다르다(위
# docstring의 Imperva 건). 요청 수도 74번에서 13번으로 줄어든다.
ORDER_LIST_URL = "https://www.lotteon.com/p/order/mylotte/orderDeliveryList"
LIST_RENDER_WAIT_MS = 8000   # 목록이 그려질 때까지 (SPA라 goto만으로는 비어 있다)
LIST_MORE_WAIT_MS = 2500     # [더보기] 누르고 다음 15건이 붙을 때까지
LIST_MORE_MAX_CLICKS = 30    # 무한정 누르지 않는다 (15건씩 -> 최대 450건)
# 목록을 훑는 값이 상세를 여는 것보다 싼 최소 건수. 몇 건 안 되면 그냥 상세를 연다.
LIST_PREFETCH_MIN_ORDERS = 5

# 이 상태로 적힌 주문은 송장번호가 아직 없다 - 상세를 열어도 '미발급'만 나온다.
# 2026-08-31 실측으로 확인한 것만 넣었다(상품준비중 2/2, 출고지시 3/3이 상세에서
# 미발급). 여기 없는 상태(예: "09/02 도착예정")는 예전처럼 상세를 열어 확인한다.
LIST_NOT_YET_STATUSES = ("상품준비중", "배송준비중", "결제완료", "입금대기",
                         "주문접수", "출고지시")

_LIST_CARDS_JS = """() => [...document.querySelectorAll('.orderGroupWrap')].map(card => ({
  odNo: ((card.querySelector('span.orderNumber') || {}).innerText || '').trim(),
  date: ((card.querySelector('span.date') || {}).innerText || '').trim(),
  statuses: [...card.querySelectorAll('span.status')].map(el => el.innerText.trim()),
  etas: [...card.querySelectorAll('p.expectingDate')].map(el => el.innerText.trim()),
}))"""

# prepare_batch가 읽어둔 목록. 컨텍스트(=이번 실행의 브라우저)별로 담는다.
# 한 공급사는 스레드 하나가 맡으므로 여기에 잠금은 필요 없다.
_listed_orders: dict[int, dict[str, dict]] = {}


def extract_od_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("odNo")
    if not values:
        raise ParseError(f"URL에서 odNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문들을 주문내역 목록에서 미리 훑어둔다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 여기서 읽어둔
    상태는 get_tracking이 상세를 열기 전에 본다 - '아직 안 나간 주문'이면
    상세를 아예 열지 않는다.

    실패하면 아무것도 읽지 않은 것과 같아서, 모든 주문이 예전처럼 상세를
    여는 경로로 간다. 그래서 여기서는 어떤 예외도 밖으로 내보내지 않는다.
    """
    wanted = set()
    for order in orders:
        try:
            wanted.add(extract_od_no(order.product_url))
        except ParseError:
            continue  # 이런 주문은 어차피 상세 경로에서 같은 이유로 실패한다
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return

    page = context.new_page()
    try:
        page.goto(ORDER_LIST_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(LIST_RENDER_WAIT_MS)
        seen: dict[str, dict] = {}
        for _ in range(LIST_MORE_MAX_CLICKS + 1):
            for card in page.evaluate(_LIST_CARDS_JS):
                if card["odNo"]:
                    seen[card["odNo"]] = card
            if not (wanted - seen.keys()):
                break
            more = page.locator(".myLotteWrap button.more").first
            if more.count() == 0 or not more.is_visible():
                break
            more.click()
            page.wait_for_timeout(LIST_MORE_WAIT_MS)

        found = {od_no: card for od_no, card in seen.items() if od_no in wanted}
        _listed_orders[id(context)] = found
        common.safe_print(f"[lotteon] 주문내역 목록에서 {len(found)}/{len(wanted)}건을 미리 확인했습니다.")
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 그냥 예전처럼 상세를 연다
        common.safe_print(f"[lotteon] 주문내역 목록을 읽지 못해 주문마다 상세를 엽니다 ({e}).")
    finally:
        page.close()


def _listed_order_date(text: str) -> date | None:
    """목록의 '2026.08.31' 표기."""
    try:
        return datetime.strptime(text.strip(), "%Y.%m.%d").date()
    except (ValueError, AttributeError):
        return None


def _raise_if_listed_settled(card: dict, od_no: str) -> None:
    """목록에 적힌 상태만으로 결론이 나면 상세를 열지 않고 여기서 끝낸다.

    결론이 안 나면(모르는 상태, 오래된 주문) 그냥 돌아가고, 호출한 쪽이
    예전처럼 주문상세를 연다.
    """
    statuses = [s for s in card.get("statuses") or [] if s]
    if not statuses:
        return
    order_date = _listed_order_date(card.get("date") or "")
    note = eta_mod.from_text(" ".join(statuses + (card.get("etas") or [])))

    # 취소/품절은 기다려도 송장이 안 나온다. 주문상태를 정확히 읽을 수 있는
    # 공급사는 NOT_YET 판정보다 먼저 본다(base.raise_if_cancelled 규칙).
    # 롯데온 주문상세에는 이 표시가 안 나와서, 목록을 봐야만 알 수 있다.
    # 목록의 한 주문이 여러 줄로 뜨기도 한다. '취소' 줄과 '준비중' 줄이 같이
    # 있으면 준비 쪽이 이겨 미발급으로 넘어간다 (base.raise_if_cancelled_any).
    try:
        raise_if_cancelled_any(statuses, od_no)
    except (OrderCancelled, TrackingNotAvailableYet) as e:
        e.order_date, e.delivery_note = order_date, note
        raise

    if not all(any(k in s for k in LIST_NOT_YET_STATUSES) for s in statuses):
        return  # 모르는 상태가 섞여 있으면 상세로 확인한다

    # 주문한 지 오래된 건은 상세까지 열어 예정 문구를 읽는다 - 사람이 '왜 아직
    # 안 나갔나'를 판단할 때 쓰는 값이라(report.stale_entries), 목록에 없는
    # 문구까지 챙겨야 한다. 주문일을 못 읽었으면 오래된 건인지 알 수 없으므로
    # 마찬가지로 상세를 연다.
    if order_date is None or order_date_mod.is_stale(order_date):
        return

    error = TrackingNotAvailableYet(
        f"아직 송장번호가 발급되지 않았습니다 (odNo={od_no}, 주문내역 '{statuses[0]}')."
    )
    error.order_date, error.delivery_note = order_date, note
    raise error


def _looks_like_login_page(page) -> bool:
    """URL만으로는 오탐이 잦아서(로그인 후에도 잠깐 거치는 리다이렉트 URL에
    "login"이 들어있는 경우가 있음), URL + 실제 비밀번호 입력창 존재 여부를
    함께 본다. 로그아웃 상태면 서버가 302로 바로 넘기므로 기다리지 않는다
    (common.looks_like_login_page).
    """
    return common.looks_like_login_page(page, lambda url: "login" in url.lower())


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
            # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
            # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
            page.wait_for_timeout(1500)
            # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인
            # 자체가 된 경우(비밀번호 변경 안내 등)가 있어, 페이지 상태를
            # alert보다 먼저 본다.
            if not _looks_like_login_page(page):
                return True
            if alerts:
                raise BlockedError(f"롯데온 자동 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1500

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

    # 주문내역 목록에서 이미 결론이 난 주문이면 상세를 열지 않는다 (prepare_batch).
    listed = _listed_orders.get(id(context), {}).get(od_no)
    if listed is not None:
        _raise_if_listed_settled(listed, od_no)

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

        # 주문상세는 자바스크립트로 그려진다 - 주문번호가 화면에 뜨면 다 그려진 것이다.
        common.wait_for_text(page, od_no)
        # 주문상세 화면을 떠나기 전에 주문일부터 읽어둔다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, lambda: _scrape_tracking_from_page(page, od_no, order_option))
    finally:
        page.close()
