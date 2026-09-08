"""하프클럽(www.halfclub.com, 트라이씨클) 공급사 어댑터.

리버스엔지니어링 결과 (2026-09-08 실측):
- 샵마인 엑셀의 "상품URL"에는 주문별 상세 주소가 들어온다:
    https://www.halfclub.com/mypage/order-state/detail?ordNo=<주문번호>
  화면은 Nuxt(Vue) 앱이고 내용은 전부 JSON API로 받아 그린다. 그래서 이 어댑터는
  **브라우저 화면을 열지 않고 API만 부른다**:
    GET https://cf-hapi.halfclub.com/order/history/orderShip/detail?ordNo=<주문번호>&countryCd=001&langCd=001&siteCd=1&deviceCd=001&mandM=halfclub
  응답 data.orderInfo.saleDetailList[] 가 상품 한 줄씩이고, 줄마다
    - deliveryInfo.cjNo   = 송장번호 (아직 없으면 deliveryInfo 자체가 없거나 값이 비어 있다)
    - deliveryInfo.dlvUrl = 택배사 코드 (굿스플로우 코드: cjgls=CJ대한통운, hanjin=한진택배 ...)
    - selectedOpt[].optValueList[].optValueNm = 옵션값 (화면은 " / "로 이어 붙인다)
    - status / dlvSta      = 주문상태 (화면 라벨은 JS의 표를 그대로 옮긴 STATUS_LABELS)
  주문일은 orderInfo.prdSalYmd("2026-09-06 19:58:46"). 없는 주문번호는 HTTP 500 +
  code ER_9999 "주문 정보가 유효하지 않습니다"로 온다.
- **주문목록 API**(GET /order/history/orderShip?pageNo=0&pageSize=100&sort=A&...)의
  항목이 상세 응답의 orderInfo와 같은 구조라(송장·택배사 코드·옵션·상태 전부 포함)
  이번에 조회할 주문이 2건 이상이면 prepare_batch가 목록을 한 번 받아 캐시해 둔다
  (29CM·신세계TV쇼핑과 같은 패턴). 캐시로 답한 주문은 요청을 안 보냈으므로
  sent_request=False로 표시해 오케스트레이터가 간격을 두지 않게 한다.
- API 인증은 쿠키가 아니라 **Authorization: Bearer <JWT>** 헤더다. JWT는 로그인
  응답(jwt.Jwt)이 www.halfclub.com 쿠키 `user_auth`(자동로그인이면 90일)에 저장되고,
  갱신용 `user_re_auth`(RefreshJwt, 만료 없음/쿠키 90일)가 함께 저장된다. 우리는
  storage_state(auth/halfclub_state.json)에 그 쿠키를 그대로 남겨 다음 실행부터
  로그인 없이 쓴다.
  * JWT는 24시간짜리다. 만료되면 API가 200 + {"code":"ER_5001"}을 주는데, 그때는
    사이트 JS와 똑같이 POST /member/members/reAuth (헤더 Bearer <RefreshJwt>, 본문
    {"RefreshJwt": ...})로 새 JWT를 받아 쿠키를 갈아 끼운다 - 실측으로 새 JWT로
    조회가 되고, 그 뒤에도 옛 JWT가 바로 죽지는 않는다.
  * **JWT에 로그인한 브라우저의 User-Agent가 박혀 있고 서버가 그것을 대조한다**
    (사이트 안내문: "로그인 중에 브라우저 정보가 변경된 경우"). UA가 한 글자라도
    다르면(Chrome/152 -> Chrome/140, HeadlessChrome) 같은 ER_5001이다. 그래서 요청마다
    JWT payload의 `agent` 값을 User-Agent 헤더로 그대로 보낸다 - 화면 없는 번들
    크로미엄에서도 이 헤더만 맞으면 200이 온다(실측).
- 로그인 화면(/login)에는 Cloudflare Turnstile이 있고 로그인 버튼을 누르면
  /api/verify-turnstile -> /api/privateChk(비밀번호 AES 암호화) -> POST
  cf-hapi.halfclub.com/member/members/signin 순으로 간다. 번들 크로미엄/Playwright가
  띄운 크롬에서는 Turnstile이 통과되지 않아 로그인 버튼 자체가 숨겨진다.
  CJ온스타일과 같은 방식으로 **우리가 직접 실행한 진짜 크롬(browser.real_chrome_cdp_context)**
  에서만 로그인한다 - 거기서는 아무것도 누를 것 없이 통과한다(실측). 입력창은 Vue라
  id가 매번 바뀌므로 placeholder로 찾는다("아이디를 입력해주세요" / "비밀번호를 입력해
  주세요"), 버튼은 button.type-login. 로그인이 끝나면 쿠키(user_auth/user_re_auth)만
  조회용 컨텍스트로 옮긴다. 사용자 요청 3번("쿠키값을 이용해서 첫 로그인부터 자동")
  대로 HALFCLUB_ID/HALFCLUB_PW로 완전 자동이고, 진짜 크롬 창은 세션이 없을 때
  (최초 1회, 또는 갱신 토큰까지 죽었을 때)만 뜬다.
- 택배사: 코드 -> 이름 표(COURIER_CODES)를 먼저 보고, 모르는 코드는 굿스플로우
  공개 API(POST trace.goodsflow.com/view/api/tracking, 로그인 없음)의
  baseData.logisticsName을 읽는다 - 배송조회 버튼이 여는 화면이 이 API로 그려진다.
  어느 쪽이든 마지막에 common.normalize_courier를 거쳐 CJ/대한통운 -> CJ대한통운,
  롯데 -> 롯데택배, DELIBOX -> 딜리박스로 맞춘다 (사용자 요청 4·5번).

주문에 상품이 여러 줄이면 (사용자 요청 6번: "주문 옵션을 비교해서 찾아줘"):
  1. 샵마인 엑셀의 "주문옵션"이 어느 줄의 옵션에만 유일하게 들어 있으면 그 줄
  2. 아니면 옥션과 같은 토큰 점수(auction.option_score)로 1등이 유일하면 그 줄
  3. 그래도 못 고르면 송장이 있는 줄을 전부 보고 송장이 하나뿐이면 그것, 서로
     다르면 사람이 보도록 ParseError
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from ..models import TrackingResult
from . import common
from .auction import option_score
from .base import (
    AdapterError,
    BlockedError,
    OrderCancelled,
    OrderNotFound,
    ParseError,
    ShipmentDelayed,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
)

load_dotenv()

DOMAINS = {"halfclub.com", "www.halfclub.com", "m.halfclub.com"}
SITE_KEY = "halfclub"

# 화면 없이 JSON 요청만 보내고 봇 확인도 없는 경로 - API 전용 사이트(무신사/4910/
# 신세계TV쇼핑)와 같은 간격. 캐시로 답한 주문에는 간격 자체가 안 붙는다.
REQUEST_GAP = (0.5, 1.2)

SITE_URL = "https://www.halfclub.com"
LOGIN_URL = SITE_URL + "/login"
LOGIN_PATH = "/login"
API_BASE = "https://cf-hapi.halfclub.com"
COMMON_QUERY = "countryCd=001&langCd=001&siteCd=1&deviceCd=001&mandM=halfclub"
DETAIL_URL = API_BASE + "/order/history/orderShip/detail?ordNo={order_no}&" + COMMON_QUERY
LIST_URL = API_BASE + "/order/history/orderShip?pageNo={page}&pageSize={rows}&sort=A&" + COMMON_QUERY
REAUTH_URL = API_BASE + "/member/members/reAuth"
GOODSFLOW_URL = "https://trace.goodsflow.com/view/api/tracking"
GOODSFLOW_MEMBER = "halfclub"

AUTH_COOKIE = "user_auth"
REFRESH_COOKIE = "user_re_auth"
COOKIE_DOMAIN = "www.halfclub.com"
COOKIE_LIFETIME_SEC = 90 * 24 * 3600  # 사이트가 자동로그인 쿠키에 주는 기간과 같다

LOGIN_ID_SELECTOR = "input[placeholder='아이디를 입력해주세요']"
LOGIN_PW_SELECTOR = "input[placeholder='비밀번호를 입력해 주세요']"
LOGIN_BUTTON_SELECTOR = "button.type-login"
LOGIN_FORM_WAIT_MS = 8 * 1000     # 로그인 주소가 된 뒤 입력창이 그려질 때까지
LOGIN_SETTLE_MS = 3 * 1000        # 프로필에 로그인이 남아 있으면 이 안에 홈으로 넘어간다
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 버튼을 누른 뒤 로그인 주소를 벗어날 때까지

# 갱신/로그인 뒤 같은 요청을 다시 시도하는 횟수 (갱신 1번 + 재로그인 1번)
MAX_AUTH_RETRIES = 2
# 만료가 이만큼 남은 JWT는 부르기 전에 미리 갱신한다 (서버 시계와의 오차 여유)
JWT_EXPIRY_MARGIN_SEC = 60

# 목록 페이지 크기와 최대 페이지 수 (실측 13건이라 보통 한 페이지로 끝난다)
LIST_ROWS_PER_PAGE = 100
LIST_MAX_PAGES = 3
# 1건이면 목록이나 상세나 요청 하나라 이득이 없다.
LIST_PREFETCH_MIN_ORDERS = 2

# 응답 code 값. ER_5001/5002/1020은 사이트 JS가 '로그인 정보가 유효하지 않다'로
# 다루는 셋이고, ER_9999는 없는 주문번호에 왔다.
AUTH_ERROR_CODES = {"ER_5001", "ER_5002", "ER_1020"}
NOT_FOUND_CODE = "ER_9999"

# 사이트 JS(ordersOption 청크)의 상태 표. e/e2는 dlvSta 10·20이면 배송중, 30이면
# 배송완료다.
STATUS_LABELS = {
    "b": "입금대기", "c": "결제완료", "d": "출고대기", "e": "배송중", "e2": "배송중",
    "f": "취소", "g": "교환", "h": "반품", "i": "구매확정", "j": "구매완료",
    "k": "사용완료", "l": "입금실패", "y": "결제완료(선물주문)", "x": "결제완료(선물거절)",
}
DELIVERED_DLV_STA = "30"
CANCELLED_STATUS = "f"

# 굿스플로우 택배사 코드. 실측한 것(cjgls/hanjin)과 굿스플로우가 공개하는 코드 중
# 샵마인이 쓸 만한 것만 적었다. 여기 없는 코드는 굿스플로우 API에서 이름을 읽는다.
COURIER_CODES = {
    "cjgls": "CJ대한통운",
    "korex": "CJ대한통운",
    "hanjin": "한진택배",
    "lotte": "롯데택배",
    "hyundai": "롯데택배",
    "logen": "로젠택배",
    "epost": "우체국택배",
    "kdexp": "경동택배",
    "daesin": "대신택배",
    "ilyang": "일양로지스",
    "chunil": "천일택배",
    "kunyoung": "건영택배",
    "hdexp": "합동택배",
    "cvsnet": "GS편의점택배",
    "cupost": "CU편의점택배",
    "delibox": "딜리박스",
}
DEFAULT_COURIER = "택배"  # 택배사명을 어디서도 못 읽었을 때만 쓰는 기본값

TRACKING_PATTERN = re.compile(r"\d{9,}")


class _AuthExpired(Exception):
    """API가 로그인 정보가 유효하지 않다고 답함 (갱신 또는 재로그인 대상)."""


@dataclass
class Session:
    jwt: str
    refresh: str
    agent: str  # JWT에 박힌 User-Agent - 요청마다 그대로 보내야 한다

    def headers(self, token: str | None = None) -> dict[str, str]:
        return {"authorization": f"Bearer {token or self.jwt}", "user-agent": self.agent}


@dataclass
class OrderRow:
    seq: int              # ordNoNm (1부터)
    option: str           # "PINK / S"
    status: str           # STATUS_LABELS를 거친 화면 라벨
    tracking_no: str      # 없으면 ""
    courier_code: str     # deliveryInfo.dlvUrl, 없으면 ""
    delayed: bool         # orderInfo.delayMail == "Y"


@dataclass
class ListedOrder:
    order_no: str
    order_date: date | None
    rows: list[OrderRow] = field(default_factory=list)


# prepare_batch가 읽어둔 주문목록. 컨텍스트(=이번 실행의 브라우저)별로 담는다.
# 한 공급사는 스레드 하나가 맡으므로 잠금은 필요 없다 (29CM/신세계TV쇼핑과 동일).
_listed_orders: dict[int, dict[str, ListedOrder]] = {}


def extract_order_no(product_url: str) -> str:
    query = parse_qs(urlparse(product_url).query)
    for key, values in query.items():
        if key.lower() == "ordno" and values and values[0].strip():
            return values[0].strip()
    raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")


# --------------------------------------------------------------------------
# 세션 (JWT 쿠키)
# --------------------------------------------------------------------------

def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001 - 깨진 토큰은 빈 payload로 보고 갱신/로그인으로 간다
        return {}


def _load_session(context: BrowserContext) -> Session | None:
    """컨텍스트 쿠키에서 JWT 세션을 읽는다. 둘 중 하나라도 없으면 None."""
    cookies = {c["name"]: c["value"] for c in context.cookies(SITE_URL)}
    jwt, refresh = cookies.get(AUTH_COOKIE), cookies.get(REFRESH_COOKIE)
    if not jwt or not refresh:
        return None
    agent = _jwt_payload(jwt).get("agent") or _jwt_payload(refresh).get("agent")
    if not agent:
        return None
    return Session(jwt=jwt, refresh=refresh, agent=agent)


def _jwt_expired(token: str) -> bool:
    exp = _jwt_payload(token).get("exp")
    try:
        return float(exp) < time.time() + JWT_EXPIRY_MARGIN_SEC
    except (TypeError, ValueError):
        return False  # 만료 정보가 없으면 일단 써 보고 ER_5001이면 갱신한다


def _store_cookie(context: BrowserContext, name: str, value: str) -> None:
    """쿠키 하나를 갈아 끼운다 - 실행이 끝날 때 storage_state로 같이 저장된다."""
    context.add_cookies([{
        "name": name, "value": value, "domain": COOKIE_DOMAIN, "path": "/",
        "expires": time.time() + COOKIE_LIFETIME_SEC, "httpOnly": False, "secure": True,
        "sameSite": "Lax",
    }])


def _refresh_session(context: BrowserContext, session: Session) -> Session | None:
    """갱신 토큰으로 새 JWT를 받는다. 갱신 토큰까지 죽었으면 None."""
    try:
        response = context.request.post(
            REAUTH_URL, headers={**session.headers(session.refresh), "content-type": "application/json"},
            data=json.dumps({"RefreshJwt": session.refresh}))
        body = response.json()
        new_jwt = ((body.get("data") or {}).get("jwt") or {}).get("Jwt")
    except Exception as e:  # noqa: BLE001 - 갱신 실패는 재로그인으로 이어질 뿐이다
        common.safe_print(f"[halfclub] 토큰 갱신 요청 실패 - 다시 로그인합니다: {e}")
        return None
    if not new_jwt:
        common.safe_print(f"[halfclub] 토큰 갱신이 거부됐습니다 - 다시 로그인합니다: {str(body)[:120]}")
        return None
    _store_cookie(context, AUTH_COOKIE, new_jwt)
    common.safe_print("[halfclub] 만료된 토큰을 갱신했습니다.")
    return Session(jwt=new_jwt, refresh=session.refresh, agent=session.agent)


def _login_with_chrome(context: BrowserContext) -> Session:
    """진짜 크롬(CDP) 창에서 자동 로그인하고 쿠키를 조회용 컨텍스트로 옮긴다."""
    login_id = os.environ.get("HALFCLUB_ID")
    login_pw = os.environ.get("HALFCLUB_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "하프클럽 로그인이 필요하지만 HALFCLUB_ID/HALFCLUB_PW 환경변수가 설정되어 있지 "
            "않습니다. .env에 추가해주세요.")
    common.safe_print("[halfclub] 로그인 세션이 없어 크롬 창을 띄워 자동 로그인합니다.")
    try:
        with browser_mod.real_chrome_cdp_context(SITE_KEY) as login_context:
            page = login_context.pages[0] if login_context.pages else login_context.new_page()
            common.goto_settled(page, LOGIN_URL)
            # 프로필에 로그인이 남아 있으면 로그인 화면이 스스로 홈으로 넘어간다.
            if not common.wait_for_url(page, lambda url: LOGIN_PATH not in urlparse(url).path,
                                       LOGIN_SETTLE_MS, poll_ms=300):
                page.wait_for_selector(LOGIN_ID_SELECTOR, state="attached", timeout=LOGIN_FORM_WAIT_MS)
                page.locator(LOGIN_ID_SELECTOR).click()
                page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=60)
                page.locator(LOGIN_PW_SELECTOR).click()
                page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=60)
                page.wait_for_timeout(500)
                # Turnstile 토큰은 버튼을 누를 때 저절로 채워진다 - 사람이 누를 것은 없다.
                page.locator(LOGIN_BUTTON_SELECTOR).click(force=True)
                if not common.wait_for_url(page, lambda url: LOGIN_PATH not in urlparse(url).path,
                                           LOGIN_WAIT_TIMEOUT_MS, poll_ms=500):
                    raise BlockedError(
                        "하프클럽 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다 - "
                        "아이디/비밀번호 또는 '사람인지 확인'을 확인해주세요.")
            # 리다이렉트가 끝나 쿠키가 다 깔린 뒤에 옮긴다.
            page.wait_for_timeout(1000)
            cookies = [c for c in login_context.cookies(SITE_URL) if c["name"] in (AUTH_COOKIE, REFRESH_COOKIE)]
            if len({c["name"] for c in cookies}) < 2:
                raise BlockedError("하프클럽 로그인 뒤에도 인증 쿠키(user_auth/user_re_auth)가 없습니다.")
            context.add_cookies(cookies)
    except AdapterError:
        raise
    except Exception as e:  # noqa: BLE001 - 무엇이든 로그인 실패는 이 사이트 전체를 막는다
        raise BlockedError(f"하프클럽 자동 로그인 중 오류: {e}") from e
    session = _load_session(context)
    if session is None:
        raise BlockedError("하프클럽 로그인 쿠키에서 토큰을 읽지 못했습니다.")
    common.safe_print("[halfclub] 자동 로그인에 성공했습니다.")
    return session


def _ensure_session(context: BrowserContext) -> Session:
    session = _load_session(context)
    if session is None:
        return _login_with_chrome(context)
    if _jwt_expired(session.jwt):
        return _refresh_session(context, session) or _login_with_chrome(context)
    return session


# --------------------------------------------------------------------------
# API 호출
# --------------------------------------------------------------------------

def _api_get(context: BrowserContext, url: str, session: Session) -> dict:
    """로그인된 상태로 API JSON을 받는다. 로그인 정보가 죽었으면 _AuthExpired."""
    response = context.request.get(url, headers=session.headers())
    text = response.text()
    try:
        body = json.loads(text)
    except ValueError as e:
        raise ParseError(f"하프클럽 API 응답이 JSON이 아닙니다 (HTTP {response.status}): {text[:120]}") from e
    code = body.get("code")
    if code in AUTH_ERROR_CODES:
        raise _AuthExpired(code)
    if code == NOT_FOUND_CODE or response.status == 500:
        raise OrderNotFound(f"하프클럽에 없는 주문입니다: {body.get('message') or text[:120]}")
    if response.status != 200 or code or "data" not in body:
        raise ParseError(f"하프클럽 API 오류 (HTTP {response.status}): {text[:200]}")
    return body["data"]


def _get_json(context: BrowserContext, url: str) -> dict:
    """세션이 죽어 있으면 갱신(없으면 재로그인)하고 같은 요청을 다시 보낸다."""
    session = _ensure_session(context)
    for attempt in range(MAX_AUTH_RETRIES + 1):
        try:
            return _api_get(context, url, session)
        except _AuthExpired as e:
            if attempt >= MAX_AUTH_RETRIES:
                raise BlockedError(f"하프클럽 로그인 정보가 계속 거부됩니다 ({e}).") from e
            if attempt == 0:
                session = _refresh_session(context, session) or _login_with_chrome(context)
            else:
                session = _login_with_chrome(context)
    raise BlockedError("하프클럽 로그인 정보가 계속 거부됩니다.")  # 도달하지 않는다


# --------------------------------------------------------------------------
# 응답 해석
# --------------------------------------------------------------------------

def _status_label(row: dict) -> str:
    status = str(row.get("status") or "")
    if status in ("e", "e2") and str(row.get("dlvSta") or "") == DELIVERED_DLV_STA:
        return "배송완료"
    return STATUS_LABELS.get(status, status)


def _option_text(row: dict) -> str:
    """사이트 JS와 같은 방식으로 옵션값을 ' / '로 이어 붙인다."""
    items = sorted(row.get("selectedOpt") or [], key=lambda o: o.get("optPrrtRnk") or 0)
    values = [str(v.get("optValueNm") or "").strip()
              for item in items for v in (item.get("optValueList") or [])]
    return " / ".join(v for v in values if v)


def _tracking_of(row: dict) -> tuple[str, str]:
    info = row.get("deliveryInfo") or {}
    raw = str(info.get("cjNo") or "").replace("-", "").strip()
    m = TRACKING_PATTERN.search(raw)
    return (m.group(0) if m else ""), str(info.get("dlvUrl") or "").strip().lower()


def _parse_date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_order(info: dict) -> ListedOrder:
    """상세 응답의 orderInfo(= 목록 항목 하나)를 상품 줄 목록으로 정리한다."""
    delayed = str(info.get("delayMail") or "").upper() == "Y"
    order = ListedOrder(order_no=str(info.get("ordNo") or ""), order_date=_parse_date(info.get("prdSalYmd")))
    for row in info.get("saleDetailList") or []:
        tracking_no, courier_code = _tracking_of(row)
        order.rows.append(OrderRow(
            seq=int(row.get("ordNoNm") or 0), option=_option_text(row), status=_status_label(row),
            tracking_no=tracking_no, courier_code=courier_code, delayed=delayed))
    return order


def _fetch_list(context: BrowserContext) -> dict[str, ListedOrder]:
    found: dict[str, ListedOrder] = {}
    for page_no in range(LIST_MAX_PAGES):
        data = _get_json(context, LIST_URL.format(page=page_no, rows=LIST_ROWS_PER_PAGE))
        items = data.get("saleHistoryInfo") or []
        for item in items:
            order = parse_order(item)
            if order.order_no:
                found.setdefault(order.order_no, order)
        if len(items) < LIST_ROWS_PER_PAGE:
            break
    return found


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문이 2건 이상이면 주문목록을 한 번 받아 캐시한다.

    실패하면 아무것도 읽지 않은 것과 같아서 모든 주문이 상세 API 경로로 간다 -
    그래서 어떤 예외도 밖으로 내보내지 않는다.
    """
    wanted = set()
    for order in orders:
        try:
            wanted.add(extract_order_no(order.product_url))
        except ParseError:
            continue
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return
    try:
        listed = _fetch_list(context)
    except Exception as e:  # noqa: BLE001 - 미리 읽기는 실패해도 주문별 경로가 있다
        common.safe_print(f"[halfclub] 주문목록 미리 읽기 실패 - 주문별로 조회합니다: {e}")
        return
    _listed_orders[id(context)] = listed
    hit = len(wanted & set(listed))
    common.safe_print(f"[halfclub] 주문목록 {len(listed)}건을 미리 읽었습니다 "
                      f"- 조회 대상 {len(wanted)}건 중 {hit}건이 목록에 있습니다.")


def _locate_order(context: BrowserContext, order_no: str) -> tuple[ListedOrder, bool]:
    """(주문, 요청을 보냈는가)"""
    cached = _listed_orders.get(id(context), {}).get(order_no)
    if cached is not None:
        return cached, False
    data = _get_json(context, DETAIL_URL.format(order_no=order_no))
    info = data.get("orderInfo") or {}
    if not info.get("ordNo"):
        raise ParseError(f"주문상세 응답에 orderInfo가 없습니다 (주문번호={order_no}).")
    return parse_order(info), True


# --------------------------------------------------------------------------
# 상품 줄 고르기 (사용자 요청 6번)
# --------------------------------------------------------------------------

def select_row(rows: list[OrderRow], order_option: str | None) -> OrderRow | None:
    """샵마인 "주문옵션"으로 상품 줄 하나를 고른다. 못 고르면 None.

    1) 정규화한 옵션이 그대로 들어 있는 줄이 유일하면 그 줄
    2) 아니면 토큰 점수(옥션과 같은 방식) 1등이 유일하고 0점이 아니면 그 줄
    """
    if len(rows) == 1:
        return rows[0]
    if not order_option:
        return None
    target = normalize_option(order_option)
    if target:
        contained = [r for r in rows if target in normalize_option(r.option)]
        if len(contained) == 1:
            return contained[0]
    scored = sorted(((option_score(order_option, r.option), i) for i, r in enumerate(rows)), reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return rows[scored[0][1]]
    return None


# --------------------------------------------------------------------------
# 택배사
# --------------------------------------------------------------------------

def _goodsflow_courier(context: BrowserContext, code: str, tracking_no: str) -> str:
    """굿스플로우 공개 API에서 택배사 이름을 읽는다 (못 읽으면 빈 문자열)."""
    try:
        response = context.request.post(
            GOODSFLOW_URL, headers={"content-type": "application/json"},
            data=json.dumps({"memberCode": GOODSFLOW_MEMBER, "logisticsCode": code, "invoiceNo": tracking_no}))
        return str(((response.json().get("baseData") or {}).get("logisticsName")) or "").strip()
    except Exception:  # noqa: BLE001 - 택배사명은 부가 정보라 조회를 깨지 않는다
        return ""


def courier_name(context: BrowserContext, code: str, tracking_no: str) -> str:
    name = COURIER_CODES.get(code) or _goodsflow_courier(context, code, tracking_no) or code
    return common.normalize_courier(name) if name else DEFAULT_COURIER


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------

def _raise_for_row(row: OrderRow, order_no: str) -> None:
    """송장이 없는 줄의 사유를 예외로 바꾼다."""
    label = row.status or "없음"
    if row.status == STATUS_LABELS[CANCELLED_STATUS]:
        raise OrderCancelled(f"취소된 주문입니다 (주문번호={order_no}, 상태={label}).")
    if row.delayed:
        raise ShipmentDelayed(f"공급사가 발송지연을 알린 주문입니다 (주문번호={order_no}, 상태={label}).")
    raise TrackingNotAvailableYet(f"아직 송장번호가 없습니다 (주문번호={order_no}, 상태={label}).")


def _lookup(context: BrowserContext, order: ListedOrder, order_option: str | None,
            sent_request: bool) -> TrackingResult:
    order_no = order.order_no
    if not order.rows:
        raise ParseError(f"주문에 상품 줄이 없습니다 (주문번호={order_no}).")

    row = select_row(order.rows, order_option)
    if row is not None:
        candidates = [row]
    else:
        # 옵션으로 특정할 수 없으면 송장이 있는 줄을 전부 보고 하나뿐인지 본다
        # (다른 어댑터와 같은 안전 규칙). 송장이 있는 줄이 없으면 전부를 상태 판정에 쓴다.
        shipped = [r for r in order.rows if r.tracking_no]
        candidates = shipped or order.rows

    try:
        for r in candidates:
            if not r.tracking_no:
                _raise_for_row(r, order_no)
    except AdapterError as e:
        e.sent_request = sent_request
        raise

    if len({r.tracking_no for r in candidates}) > 1:
        raise ParseError(
            f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
            "주문옵션으로 어느 상품인지 고르지 못했습니다. 상품별로 나눠 배송된 것으로 보입니다.")
    chosen = candidates[0]
    courier = courier_name(context, chosen.courier_code, chosen.tracking_no)
    return TrackingResult(tracking_no=chosen.tracking_no, courier=courier, sent_request=sent_request)


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    order, sent_request = _locate_order(context, order_no)
    return attach_order_date(order.order_date, lambda: _lookup(context, order, order_option, sent_request))
