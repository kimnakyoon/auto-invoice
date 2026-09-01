"""29CM(29cm.co.kr) 공급사 어댑터.

리버스엔지니어링 결과(2026-09-01 실측):
- 샵마인 엑셀의 상품URL: https://www.29cm.co.kr/order/my-order/detail/<주문번호>
  (주문번호는 숫자. 화면에 보이는 "ORD20260901-xxxxxxx"는 별도의 표시용
  일련번호고, URL의 숫자가 API 주소에 그대로 들어간다.)
- 주문상세 화면이 부르는 JSON API를 직접 호출한다(페이지를 열지 않는다):
    GET https://apihub.29cm.co.kr/api/v2/order/orders/my-order/<주문번호>/
  - 로그인이 안 되어 있으면 401 {"errors":{"code":"E002",...}}
  - 없는 주문번호(또는 남의 주문)면 404 {"errors":{"code":"E004",...}}
  - manages[].order_delivery_no.details[]에 delivery_company_name("CJ대한통운")
    과 invoice_no(송장번호)가 들어 있다. 아직 안 나간 주문은 details가 빈
    배열이고 combine_invoice_no/combine_delivery_company도 null이다.
  - 반품접수/전체취소 주문은 manages 자체가 빈 배열로 온다 - 이때는 최상위
    order_status_description("반품접수"/"전체취소")으로 판별한다.
  - 주문일은 최상위 insert_timestamp("2026-09-01 07:36:32")에서 읽는다.
  - 발송 예정은 manages[].shipped_out_expect_date("09/02(수)")로 온다.
- 로그인: 29CM는 무신사 통합계정 체계다. 사용자 계정(howk93)은 통합계정이라
  29CM 자체 이메일 로그인(auth.29cm.co.kr/email-login)에서는
  INVALID_CREDENTIALS로 거부된다(실측 - 실패 카운트까지 올라가니 시도하면
  안 된다). 대신 로그인 화면의 "무신사 통합계정 가입 및 로그인" 버튼을 눌러
  member.one.musinsa.com 로그인 폼(무신사 어댑터와 동일한 폼)을 거치면
  OAuth로 29CM 세션이 발급된다. headless 번들 크로미엄으로도 전 과정이
  통과하는 것을 확인했다(2026-09-01) - 진짜 크롬을 띄울 필요가 없다.
- 아이디/비밀번호는 29CM_ID/29CM_PW를 먼저 보고, 없으면 같은 통합계정이므로
  MUSINSA_ID/MUSINSA_PW를 그대로 쓴다.
- 한 번 로그인하면 세션이 auth/29cm_state.json에 저장되어(storage_state)
  다음 실행부터는 로그인 없이 API 조회만 한다.
- 택배사 이름은 API가 한글 정식 명칭("CJ대한통운")으로 주지만, 표기가 흔들려도
  (CJ/대한통운/롯데/DELIBOX 등) 샵마인이 아는 이름이 되도록 항상
  common.normalize_courier를 태운다.
- 한 주문에 상품이 여러 개면 샵마인 엑셀의 "주문옵션"으로 어느 상품인지
  고른다(order_item_no.option_value가 "[사이즈]08(260)" 형태라 표기 잡음을
  지우고 비교한다). 특정하지 못하면 다른 어댑터와 같은 안전 규칙으로,
  송장번호가 전부 같을 때만 쓰고 다르면 사람이 확인하도록 예외를 던진다.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
)
from .musinsa import (
    AUTOLOGIN_CHECKBOX_SELECTOR,
    AUTOLOGIN_LABEL_SELECTOR,
    COURIER_CODE_MAP,
    LOGIN_ID_PLACEHOLDER,
    LOGIN_PW_PLACEHOLDER,
    LOGIN_SUBMIT_SELECTOR,
    RECAPTCHA_FLAG_SELECTOR,
)

load_dotenv()

DOMAINS = {"29cm.co.kr", "www.29cm.co.kr", "shop.29cm.co.kr", "m.29cm.co.kr"}
SITE_KEY = "29cm"

ORDER_API_URL = "https://apihub.29cm.co.kr/api/v2/order/orders/my-order/{order_no}/"
ORDER_DETAIL_URL = "https://www.29cm.co.kr/order/my-order/detail/{order_no}"

# 로그인 선택 화면(auth.29cm.co.kr)에서 무신사 통합 로그인으로 들어가는 버튼.
ONE_LOGIN_BUTTON_TEXT = "무신사 통합계정 가입 및 로그인"

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인은 사람을 기다리지 않으니 짧게

# 기다리면 송장이 나오는 상태에 들어 있는 말 (order_item_delivery_status_description
# 또는 order_status_description). "상품준비"/"결제완료"/"입금대기" 등.
NOT_YET_KEYWORDS = ("준비", "결제", "입금", "주문확인")
# 아무리 기다려도 송장이 나오지 않는 상태에 들어 있는 말. "전체취소"/"반품접수"/
# "교환접수" 등. 교환은 재배송 송장이 새로 달리지만 원 송장을 올리면 안 되는
# 것은 같아서 사람이 확인하도록 넘긴다.
CANCELLED_KEYWORDS = ("취소", "반품", "품절", "교환")


def _courier(raw: str) -> str:
    """택배사 이름을 샵마인이 아는 이름으로.

    29CM는 실측상 한글 정식 명칭("CJ대한통운")으로 주지만, 같은 무신사 계열이라
    코드("LOTTE"/"CJGLS")로 올 가능성에 대비해 무신사의 코드표를 먼저 태운다.
    표에 없으면 이름 그대로 두고 공통 정규화(CJ/대한통운/롯데/DELIBOX 등)만 한다.
    """
    return common.normalize_courier(COURIER_CODE_MAP.get(raw, raw))


def _login_id() -> str | None:
    return os.environ.get("29CM_ID") or os.environ.get("MUSINSA_ID")


def _login_pw() -> str | None:
    return os.environ.get("29CM_PW") or os.environ.get("MUSINSA_PW")


def extract_order_no(product_url: str) -> str:
    """상품URL에서 주문번호를 뽑는다 (/order/my-order/detail/<숫자>)."""
    segments = [s for s in urlparse(product_url).path.split("/") if s]
    if segments and segments[-1].isdigit() and "order" in segments:
        return segments[-1]
    raise ParseError(f"URL에서 주문번호를 찾을 수 없습니다: {product_url}")


def _fetch_order(context: BrowserContext, order_no: str) -> dict | None:
    """주문상세 JSON. 로그인이 필요하면 None을 돌려준다(401)."""
    resp = context.request.get(ORDER_API_URL.format(order_no=order_no))
    if resp.status == 401:
        return None
    if resp.status == 404:
        raise OrderNotFound(f"29CM에 이 주문번호가 없습니다 (주문번호={order_no}).")
    if "json" not in resp.headers.get("content-type", ""):
        # 봇 차단 페이지 등 JSON이 아닌 응답 - 로그인 문제로 보고 로그인 경로로
        # 넘긴다(로그인 후에도 그러면 거기서 BlockedError가 난다).
        return None
    if not resp.ok:
        raise ParseError(f"29CM 주문 조회가 실패했습니다 (주문번호={order_no}, HTTP {resp.status}).")
    return resp.json()


def _looks_like_29cm_login(page) -> bool:
    """29CM는 로그인이 필요하면 서버가 auth.29cm.co.kr로 307으로 넘긴다."""
    return "auth.29cm.co.kr" in page.url or "member.one.musinsa.com" in page.url


def _recaptcha_required(page) -> bool:
    locator = page.locator(RECAPTCHA_FLAG_SELECTOR)
    if locator.count() == 0:
        return False
    try:
        return (locator.first.get_attribute("value") or "").strip().lower() == "true"
    except Exception:  # noqa: BLE001 - 못 읽으면 안 켜진 것으로 본다
        return False


def _check_autologin(page) -> None:
    """무신사 폼의 "자동 로그인"을 켠다 (best effort, musinsa.py와 동일)."""
    try:
        checkbox = page.locator(AUTOLOGIN_CHECKBOX_SELECTOR)
        if checkbox.count() == 0 or checkbox.first.is_checked():
            return
        page.locator(AUTOLOGIN_LABEL_SELECTOR).first.click()
    except Exception:  # noqa: BLE001 - 체크박스 하나 때문에 로그인을 깨지 않는다
        pass


def _goto_musinsa_form(page, order_no: str) -> None:
    """주문상세를 열어 로그인 선택 화면을 거쳐 무신사 통합 로그인 폼까지 간다."""
    page.goto(ORDER_DETAIL_URL.format(order_no=order_no), wait_until="domcontentloaded")
    if not _looks_like_29cm_login(page):
        return  # 이미 로그인되어 있었음
    if "member.one.musinsa.com" not in page.url:
        # 로그인 선택 화면은 Next.js로 그려서 domcontentloaded 직후에는 버튼이
        # 아직 없다 - 나타날 때까지 기다린다.
        button = page.get_by_text(ONE_LOGIN_BUTTON_TEXT)
        try:
            button.first.wait_for(state="visible", timeout=common.RENDER_WAIT_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - 끝내 안 나오면 구조가 바뀐 것이다
            raise BlockedError(
                "29CM 로그인 화면에서 무신사 통합 로그인 버튼을 찾지 못했습니다 "
                "(화면 구조가 바뀐 것으로 보입니다).") from exc
        button.first.click()
        # 무신사 로그인 폼으로 자바스크립트 리다이렉트되기를 기다린다.
        if not common.wait_for_url(page, lambda url: "member.one.musinsa.com" in url,
                                   timeout_ms=15_000, poll_ms=200):
            raise BlockedError("무신사 통합 로그인 화면으로 넘어가지 못했습니다.")
        # 폼이 그려질 시간을 준다.
        page.wait_for_selector(LOGIN_SUBMIT_SELECTOR, state="attached",
                               timeout=common.RENDER_WAIT_TIMEOUT_MS)


def _auto_login(page) -> bool:
    """29CM_ID/PW(없으면 MUSINSA_ID/PW)로 무신사 통합 로그인을 자동 진행한다.

    비밀번호가 없으면 False - 호출자가 수동 로그인 대기로 넘어간다.
    무신사 어댑터와 같은 폼이라 실패도 같은 방식(alert)으로 온다.
    """
    login_id = _login_id()
    login_pw = _login_pw()
    if not login_id or not login_pw:
        return False

    if _recaptcha_required(page):
        raise BlockedError(
            "무신사 통합 로그인이 봇 확인(reCAPTCHA)을 요구하고 있어 자동 로그인을 "
            "할 수 없습니다 - 뜬 브라우저 창에서 직접 로그인해주세요.")

    alerts: list[str] = []

    def _on_dialog(dialog) -> None:
        alerts.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _on_dialog)
    try:
        page.get_by_placeholder(LOGIN_ID_PLACEHOLDER).fill(login_id)
        page.get_by_placeholder(LOGIN_PW_PLACEHOLDER, exact=True).fill(login_pw)
        _check_autologin(page)
        page.locator(LOGIN_SUBMIT_SELECTOR).first.click()

        elapsed_ms = 0
        while elapsed_ms < AUTO_LOGIN_WAIT_TIMEOUT_MS:
            page.wait_for_timeout(1200)
            # 로그인에 성공하면 무신사 폼 -> auth.29cm.co.kr/oauth-callback ->
            # www.29cm.co.kr 순서로 돌아온다. 로그인 화면 계열을 다 벗어나야
            # 세션 쿠키까지 발급된 것이다.
            if not _looks_like_29cm_login(page):
                return True
            if alerts:
                raise BlockedError(f"무신사 통합 로그인이 거부됐습니다: {alerts[0].strip()}")
            elapsed_ms += 1200

        if _recaptcha_required(page):
            raise BlockedError(
                "무신사 통합 로그인 도중 봇 확인(reCAPTCHA)이 켜졌습니다 "
                "- 뜬 브라우저 창에서 직접 로그인해주세요.")
        raise BlockedError(
            "무신사 통합 로그인 후에도 로그인 화면에서 벗어나지 못했습니다 "
            "(추가 본인인증을 요구받았을 수 있습니다 - 브라우저 창을 확인해주세요).")
    finally:
        page.remove_listener("dialog", _on_dialog)


def _ensure_logged_in(context: BrowserContext, order_no: str, headless: bool) -> None:
    """API가 401을 줬을 때만 호출된다. 성공하면 storage_state를 저장한다."""
    if headless and not _login_pw():
        raise BlockedError(
            "29CM 로그인이 필요합니다. .env에 29CM_ID/29CM_PW(또는 MUSINSA_ID/"
            "MUSINSA_PW)를 넣어두면 자동으로 로그인합니다. 아니면 --headless 없이 "
            "실행해 수동으로 로그인해주세요.")

    page = context.new_page()
    try:
        _goto_musinsa_form(page, order_no)
        if not _looks_like_29cm_login(page):
            return  # 이미 로그인되어 있었음 (레이스 컨디션 등 방어)

        if _auto_login(page):
            common.safe_print("[29cm] 로그인 세션이 없어 무신사 통합계정으로 자동 로그인했습니다.")
        else:
            if headless:  # pragma: no cover - 위에서 걸러지지만 방어적으로 남긴다
                raise BlockedError("29CM 로그인이 필요합니다. --headless 없이 실행해주세요.")
            common.prefill_login_id(
                page, page.get_by_placeholder(LOGIN_ID_PLACEHOLDER), _login_id())
            common.safe_print("[29cm] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
            common.safe_print("[29cm] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
            if not common.wait_for_manual_login(
                    page, lambda: _looks_like_29cm_login(page), LOGIN_WAIT_TIMEOUT_MS,
                    poll_ms=1200):
                raise BlockedError("29CM 로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")

        context.storage_state(path=str(browser_mod.state_path(SITE_KEY)))
    finally:
        page.close()


def _order_date_of(data: dict) -> date | None:
    """주문일 (최상위 insert_timestamp). 못 읽으면 None."""
    raw = str(data.get("insert_timestamp") or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        return None


def _delivery_note_of(manages: list[dict]) -> str | None:
    """'09/02(수) 이내 발송 예정' 같은 안내 문구. 없으면 None."""
    for manage in manages:
        expect = str(manage.get("shipped_out_expect_date") or "").strip()
        if expect:
            return f"{expect} 이내 발송 예정"
        due = str(manage.get("shipping_out_due_timestamp") or "").strip()
        if due:
            return f"{due[:10]} 이내 발송 예정"
    return None


def _option_text(manage: dict) -> str:
    """이 상품의 옵션 표기 ("[사이즈]08(260)"). 상품명까지 붙여서 비교 폭을 넓힌다."""
    item = manage.get("order_item_no") or {}
    option = str(item.get("option_value") or "").strip()
    name = str(item.get("item_name") or "").strip()
    return f"{name} {option}".strip()


def _status_text(manage: dict) -> str:
    """이 상품의 상태 표기. 취소 상태("정상"이 아니면)를 배송 상태보다 먼저 본다."""
    cancel = str(manage.get("order_item_cancel_status_description") or "").strip()
    delivery = str(manage.get("order_item_delivery_status_description") or "").strip()
    if cancel and cancel != "정상":
        return cancel
    return delivery or "알 수 없음"


def _select_by_order_option(manages: list[dict], order_option: str | None) -> list[dict] | None:
    """샵마인 엑셀의 "주문옵션"과 맞는 상품이 딱 하나면 그것만 본다.

    표기 잡음(공백/구분자/대괄호)을 지우고 비교하고, 한쪽이 다른 쪽을 포함하는
    경우도 같은 것으로 본다. 0개거나 2개 이상이면 None - 호출자가 송장번호를
    서로 비교하는 안전 규칙으로 넘어간다.
    """
    target = normalize_option(order_option)
    if not target or len(manages) <= 1:
        return None
    candidates = []
    for manage in manages:
        value = normalize_option(_option_text(manage))
        if value and (value == target or value in target or target in value):
            candidates.append(manage)
    return candidates if len(candidates) == 1 else None


def _live_details(manage: dict) -> list[dict]:
    """이 상품의 유효한 배송 건들 - 취소/반품/삭제된 송장은 뺀다."""
    delivery = manage.get("order_delivery_no") or {}
    return [
        d for d in (delivery.get("details") or [])
        if str(d.get("invoice_no") or "").strip()
        and d.get("is_cancel") != "T" and d.get("is_return") != "T" and d.get("is_deleted") != "T"
    ]


def _raise_by_status(statuses: list[str], order_no: str) -> None:
    """송장이 하나도 없을 때, 상태 표기로 '아직 미발급'인지 '취소'인지 가른다.

    준비/결제 계열이 하나라도 있으면 그쪽이 이긴다 - 기다리면 송장이 나오는
    주문을 사람이 처리할 목록으로 보내지 않기 위해서다 (base.py의 규칙과 동일).
    """
    joined = " / ".join(statuses) or "알 수 없음"
    if any(k in s for s in statuses for k in NOT_YET_KEYWORDS):
        raise TrackingNotAvailableYet(
            f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no}, 상태={joined}).")
    if any(k in s for s in statuses for k in CANCELLED_KEYWORDS):
        raise OrderCancelled(
            f"주문 상태가 {joined} 입니다 (주문번호={order_no}) - 취소/반품/교환 주문인지 확인해주세요.")
    raise ParseError(
        f"송장번호가 비어 있는데 상태를 알 수 없습니다 (주문번호={order_no}, 상태={joined}).")


def _tracking_from_order(data: dict, order_no: str, order_option: str | None) -> TrackingResult:
    manages = data.get("manages")
    if manages is None:
        raise ParseError(f"29CM 주문 응답 구조가 예상과 다릅니다 (주문번호={order_no}).")
    if not manages:
        # 반품접수/전체취소 주문은 manages가 빈 배열로 온다 (실측) - 최상위
        # 상태로 판별한다.
        _raise_by_status([str(data.get("order_status_description") or "").strip()], order_no)

    # 옵션으로 어느 상품인지 확정되면 그것만 본다 - 같은 주문의 다른 상품이
    # 이미 나갔더라도, 우리가 올려야 하는 상품의 송장이 아니면 안 된다.
    matched = _select_by_order_option(manages, order_option)
    if matched is not None:
        manages = matched

    # 취소/반품된 상품 줄은 송장 후보에서 뺀다 (반품 건에는 처음 나갈 때의
    # 송장이 그대로 남아 있어서 상태를 먼저 봐야 한다).
    live = [m for m in manages
            if not any(k in _status_text(m) for k in CANCELLED_KEYWORDS)]
    if not live:
        _raise_by_status([_status_text(m) for m in manages], order_no)

    found = {
        (str(d["invoice_no"]).strip(),
         _courier(str(d.get("delivery_company_name") or "").strip()))
        for m in live for d in _live_details(m)
    }
    if not found:
        _raise_by_status([_status_text(m) for m in live], order_no)
    if len(found) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "상품별로 나눠 배송된 것으로 보입니다.")

    tracking_no, courier = next(iter(found))
    return TrackingResult(tracking_no=tracking_no, courier=courier)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True,
    order_option: str | None = None,
) -> TrackingResult:
    order_no = extract_order_no(product_url)

    data = _fetch_order(context, order_no)
    if data is None:
        _ensure_logged_in(context, order_no, headless)
        data = _fetch_order(context, order_no)
        if data is None:
            raise BlockedError("29CM 로그인 후에도 여전히 로그인이 필요합니다.")

    manages = data.get("manages") or []
    return attach_order_date(
        _order_date_of(data),
        lambda: _tracking_from_order(data, order_no, order_option),
        delivery_note=_delivery_note_of(manages),
    )
