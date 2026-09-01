"""옥션(escrow.auction.co.kr) 공급사 어댑터.

리버스엔지니어링 결과:
- 다른 공급사와 달리 샵마인 엑셀의 "상품URL" 컬럼에 **주문별 URL이 아니라 목록
  페이지 URL**(https://escrow.auction.co.kr/Close/OrderProcessList.aspx)이
  들어있다. 옥션 화면에서 주문상세/배송조회가 전부 자바스크립트 팝업이라
  주소창에 주문별 주소가 뜨지 않기 때문이다. 그래서 이 어댑터는 URL만으로는
  주문을 특정할 수 없고, 주문내역 목록을 훑어서 "주문옵션 + 수령인"으로 어느
  주문인지 찾아낸다 (사용자 요청 그대로).
  다만 팝업이 실제로 여는 주소는 알아냈으므로, 나중에 상품URL에 주문번호가
  들어오면(아래 두 형태 중 아무거나) 목록을 훑지 않고 바로 조회한다:
    https://escrow.auction.co.kr/Close/OrderProcessDetailLayer.aspx?order_no=<주문번호>
    https://tracking.auction.co.kr/?orderNo=<주문번호>
- 로그인이 안 되어 있으면 signin.auction.co.kr/Authenticate/MobileLogin.aspx로
  리다이렉트된다. 로그인 폼 셀렉터: 아이디 input#typeMemberInputId, 비밀번호
  input#typeMemberInputPassword, 로그인 버튼 button#btnLogin (지마켓과 같은
  이베이코리아 통합 로그인이다). 사용자가 "첫 로그인부터 쿠키로 자동 로그인"을
  요청했고, 실제로 아이디+비밀번호를 채우고 로그인 버튼을 자동 클릭해도 캡차나
  봇 확인에 막히지 않는 것을 확인했다 (SSG/더현대/NS홈쇼핑/11번가와 동일한
  패턴). 그래서 AUCTION_ID/AUCTION_PW 환경변수로 완전 자동 로그인하고, 로그인
  세션은 storage_state(auth/auction_state.json)에 저장되어 이후 실행부터는
  쿠키만으로 바로 조회된다.
  (같은 이베이코리아인 지마켓은 Cloudflare 봇 확인이 떴었는데, 옥션 로그인에서는
  뜨지 않았다. 그래도 뜰 경우를 대비해 목록을 처음 열 때 감지해서 BlockedError로
  알린다.)
- 주문내역 목록(OrderProcessList.aspx)은 한 번에 10건만 그리고, 하단
  "N개 더보기"(#divBottomMoreBar a.load-more)를 누르면 AJAX로 덧붙는다. 그 AJAX는
  클릭 시점의 hidden input #hidSearchTerm 값을 조회기간으로 쓰기 때문에, 그 값을
  "1M"으로 바꿔두면 1개월치만 덧붙는다(기본값은 "3M"). 주문 한 건이
  <tr id="tr<주문번호>">이고, 그 안에 주문일(td.date-payment-num .date-num strong),
  상품명(.product-name), 주문옵션(ul.product-order-option), 주문상태
  (td.status strong), 배송조회 링크(TraceItemPopup)가 들어있다.
- 배송조회 팝업의 실제 주소는 https://tracking.auction.co.kr/?orderNo=<주문번호>
  이고, Next.js 페이지라 <script id="__NEXT_DATA__">에 구조화된 값이 서버에서
  이미 렌더링되어 들어있다(=페이지가 뜨는 즉시 읽을 수 있다):
  props.pageProps.initialState.shippingInfo 의 invoiceNo(송장번호 배열),
  shippingCompany(택배사명), orderNo(검증용). 화면 텍스트
  (span.text__delivery-cooper, "CJ대한통운 501707423071")를 파싱하는 것보다
  정확해서 JSON을 우선 쓰고, 없으면 그 span을 대체 경로로 쓴다.
- 수령인 확인은 주문상세 레이어(OrderProcessDetailLayer.aspx?order_no=...)의
  배송지정보 표에서 "받으시는 분" 행을 읽는다. 오래된 주문은 "임*미"처럼
  마스킹되어 나오는 경우가 있어, 마스킹된 이름도 비교할 수 있게 처리했다.

주문을 특정하는 방법 (사용자 요청: "주문 옵션을 비교해서 찾고, 그런 경우에는
수령인 이름하고 같이 비교해줘"):
  1. 목록의 각 주문옵션과 샵마인 엑셀의 "주문옵션"을 토큰 단위로 비교해 점수를
     매긴다. 표기가 서로 달라서("95" vs "사이즈 / 095", "S(090) 차콜 S(090)" vs
     "(60)Charcoal / S(090)") 단순 포함 검사로는 못 찾기 때문에, 구분자/괄호를
     쪼개고 숫자의 앞자리 0을 떼서 비교한다.
  2. 실제 주문내역을 받아보니 "RBK / 260 / 40,000원 / 1개"처럼 **완전히 같은
     옵션이 5건 넘게 반복**됐다. 그래서 옵션 점수만으로는 절대 확정하지 않고,
     점수가 높은 순서대로 주문상세를 열어 "받으시는 분"이 샵마인 엑셀의 "수령인"과
     같은 주문을 골라 확정한다.
  3. 수령인 컬럼이 없으면(선택 컬럼이라 없을 수 있다) 옵션 점수 1등이 유일할
     때만 쓰고, 동점이면 사람이 확인하도록 예외를 던진다.

속도에 대해 (사용자 요청: "이 화면에서 속도가 너무 느려"):
  처음에는 다른 어댑터를 따라 페이지를 열 때마다 고정으로 기다렸는데(로그인
  확인 1.5초 + 렌더링 0.8초), 실제로 재보니 주문상세 페이지 자체는 0.2~1.2초면
  다 뜨고 **느린 건 전부 그 고정 대기였다**(3건에 10.2초 -> 2.4초). 그래서
  이 어댑터는 고정 대기를 쓰지 않는다:
    - 로그인 여부는 리다이렉트된 주소(page.url)로 즉시 판단한다.
    - 배송조회의 송장정보는 서버 렌더링된 __NEXT_DATA__라 바로 읽는다.
    - "더보기"는 고정 대기 대신 주문 행 수가 늘어날 때까지만 기다린다.
    - 수령인 확인은 페이지를 매번 새로 만들지 않고 하나를 재사용한다.
    - 목록/상세 화면의 이미지·폰트는 어차피 안 보므로 받지 않는다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page

from .. import browser as browser_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    BlockedError,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    raise_if_cancelled,
)

load_dotenv()

DOMAINS = {
    "escrow.auction.co.kr",
    "auction.co.kr",
    "www.auction.co.kr",
    "myauction.auction.co.kr",
    "mmya.auction.co.kr",
    "tracking.auction.co.kr",
}
SITE_KEY = "auction"

# 오케스트레이터에게 "이 어댑터는 수령인 이름도 필요하다"고 알리는 표시.
# 옥션은 상품URL만으로 주문을 특정할 수 없어 수령인까지 봐야 하는 유일한
# 공급사라, 다른 12개 어댑터의 시그니처를 건드리지 않고 이 어댑터에만 추가
# 인자를 넘기려고 둔 플래그다 (orchestrator.py 참고).
WANTS_RECIPIENT_NAME = True

ORDER_LIST_URL = "https://escrow.auction.co.kr/Close/OrderProcessList.aspx"
ORDER_DETAIL_URL = "https://escrow.auction.co.kr/Close/OrderProcessDetailLayer.aspx?order_no={order_no}"
TRACE_URL = "https://tracking.auction.co.kr/?orderNo={order_no}"

LOGIN_ID_SELECTOR = "#typeMemberInputId"
LOGIN_PW_SELECTOR = "#typeMemberInputPassword"
LOGIN_BUTTON_SELECTOR = "#btnLogin"
LOGIN_HOST = "signin.auction.co.kr"
LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 자동 로그인 후 리다이렉트 대기 최대 30초

# 조회기간: 최근 1개월 (사용자 요청). 목록 페이지의 hidden input을 이 값으로
# 바꿔두면 "더보기" AJAX가 1개월치만 가져온다.
SEARCH_TERM_SELECTOR = "#hidSearchTerm"
SEARCH_TERM_ONE_MONTH = "1M"
# 화면의 조회기간 설정과 무관하게, 실제로 쓸 주문은 코드에서 한 번 더 이 기간으로
# 걸러낸다 (사이트 필터 동작에 기대지 않기 위해서다). 달마다 길이가 달라
# 넉넉하게 31일로 둔다.
LIST_PERIOD_DAYS = 31

ORDER_ROW_SELECTOR = "tr[id^='tr']"
MORE_BAR_SELECTOR = "#divBottomMoreBar a.load-more"
MORE_WAIT_TIMEOUT_MS = 15 * 1000
MAX_MORE_CLICKS = 10  # 1개월치면 보통 1~2번이면 끝난다 (무한루프 방지용 상한)

# 목록의 주문 행 하나에서 필요한 값을 한 번에 뽑아온다. 행마다 셀렉터를
# 따로 물어보면(주문 1건당 5번 왕복) 그것만으로 몇 초가 걸린다.
PARSE_ORDER_ROWS_JS = """() => {
    const rows = [];
    for (const tr of document.querySelectorAll("tr[id^='tr']")) {
        if (!/^tr\\d+$/.test(tr.id)) continue;
        const text = (sel) => (tr.querySelector(sel)?.textContent || '').trim().replace(/\\s+/g, ' ');
        rows.push({
            orderNo: tr.id.slice(2),
            orderDate: text('td.date-payment-num .date-num strong'),
            option: text('ul.product-order-option'),
            productName: text('.product-name'),
            status: text('td.status strong'),
            hasTracking: (tr.querySelector('td.status')?.innerHTML || '').includes('TraceItemPopup'),
        });
    }
    return rows;
}"""

RECIPIENT_LABEL = "받으시는 분"
DETAIL_TABLE_SELECTOR = "table.order-detail-table"
READ_RECIPIENT_JS = """() => {
    for (const tr of document.querySelectorAll('table.order-detail-table tr')) {
        const th = tr.querySelector('th');
        if (th && th.textContent.trim() === '받으시는 분')
            return (tr.querySelector('td')?.textContent || '').trim();
    }
    return '';
}"""
# 옵션 점수가 높은 순서대로 이만큼만 주문상세를 열어 수령인을 확인한다.
MAX_RECIPIENT_LOOKUPS = 15

DELIVERY_TEXT_SELECTOR = "span.text__delivery-cooper"
NEXT_DATA_SELECTOR = "#__NEXT_DATA__"

BOT_CHECK_PATTERNS = ["사람인지 확인", "봇(Bot)이란", "로봇이 아닙니다"]

# 요청 간격 (오케스트레이터가 기본 1.5~4초 대신 쓴다). 이베이코리아(옥션/지마켓)는
# 주문 사이 간격이 짧으면 "로봇이 아닙니다" 봇 확인이 뜬다(2026-09-01 사용자 관찰).
# 기본 간격은 조회 한 건에 걸리는 시간보다 짧아 사실상 쉬는 시간이 0이었다 -
# 간격이 '요청 시작 시각' 기준이라, 조회가 몇 초 걸려도 그 사이에 몇 초는 쉬도록
# 여유 있게 잡는다.
REQUEST_GAP = (6.0, 12.0)
# 목록의 주문상태가 이 값이면 아직 발송 전이라 송장번호가 없는 게 정상이다.
NOT_YET_STATUSES = ["입금확인중", "결제완료", "배송준비중", "상품준비중", "주문확인중"]

DEFAULT_COURIER = "택배"  # 택배사명을 못 읽었을 때만 쓰는 기본값

# 옥션 주문옵션은 "색상 / 사이즈 / 38,500원 / 1개"처럼 슬래시로 구분된다.
_SEGMENT_SPLIT = re.compile(r"[/|·]")
# 세그먼트 안에서 다시 단어로 쪼갤 때 쓰는 구분자
_WORD_SPLIT = re.compile(r"[\s,]+")
# 단어 안의 영숫자/한글 덩어리 (괄호·하이픈 등으로 붙어있는 것을 떼어낸다)
_CHUNK_PATTERN = re.compile(r"[0-9]+|[a-zA-Z]+|[가-힣]+")
# 구분자를 다 지우고 "통째로" 비교할 때 쓰는 패턴
_NOISE_PATTERN = re.compile(r"[^0-9a-zA-Z가-힣]+")
# "38,500원", "1개" 같은 가격/수량 세그먼트는 옵션 비교에서 제외한다
# (모든 주문에 다 붙어있어 비교에 도움이 안 되고 오히려 오탐을 만든다).
_PRICE_OR_QTY = re.compile(r"^[0-9,]+(원|개)$")
# 어느 주문에나 붙는 옵션 이름들 - 있으면 아무 주문이나 매칭되므로 제외한다.
_OPTION_STOPWORDS = {
    "사이즈",
    "신발사이즈",
    "선택",
    "색상",
    "컬러",
    "칼라",
    "옵션",
    "수량",
    "단일상품",
    "단일",
    "공통",
    "통합색상",
    "기타",
}


def _canonical_token(text: str) -> str:
    """구분자를 지우고 소문자로. 숫자만 남으면 앞자리 0을 떼서 "095"와 "95"를 같게 본다."""
    token = _NOISE_PATTERN.sub("", text).lower()
    if token.isdigit():
        token = token.lstrip("0") or "0"
    return token


def option_tokens(text: str | None) -> set[str]:
    """주문옵션 문자열에서 비교용 토큰을 뽑는다.

    같은 상품인데도 샵마인 쪽은 "S(090) 차콜 S(090)", 옥션 쪽은
    "(60)Charcoal / S(090) / 29,900원 / 1개" 처럼 표기가 달라서, 세 단계로
    쪼개서 전부 토큰에 넣는다:
      1) 세그먼트 통째로  -> "s090"
      2) 세그먼트 안의 단어 -> "s090", "차콜"
      3) 단어 안의 영숫자 덩어리 -> "s", "90"
    이렇게 하면 긴 토큰이 맞을수록 점수가 높아져(길이로 가중치를 준다) 표기가
    정확히 같은 주문이 자연스럽게 1등이 된다.
    """
    tokens: set[str] = set()
    for segment in _SEGMENT_SPLIT.split(text or ""):
        segment = segment.strip()
        if not segment or _PRICE_OR_QTY.match(segment.replace(" ", "")):
            continue
        for candidate in [segment, *_WORD_SPLIT.split(segment)]:
            token = _canonical_token(candidate)
            if token and token not in _OPTION_STOPWORDS:
                tokens.add(token)
            for chunk in _CHUNK_PATTERN.findall(candidate):
                chunk_token = _canonical_token(chunk)
                if chunk_token and chunk_token not in _OPTION_STOPWORDS:
                    tokens.add(chunk_token)
    return tokens


def option_score(shopmine_option: str | None, auction_option: str | None) -> int:
    """겹치는 토큰의 글자수 합 - 길게 겹칠수록(=표기가 정확히 같을수록) 높다."""
    shared = option_tokens(shopmine_option) & option_tokens(auction_option)
    return sum(len(token) for token in shared)


# --------------------------------------------------------------------------
# 수령인 비교
# --------------------------------------------------------------------------


def recipient_matches(auction_name: str, shopmine_name: str) -> bool:
    """"받으시는 분"과 샵마인 "수령인"이 같은 사람인지.

    오래된 주문은 옥션이 "임*미"처럼 가운데를 가려서 보여주기 때문에, '*'가
    있으면 글자수와 가려지지 않은 자리만 비교한다.
    """
    left = re.sub(r"\s+", "", auction_name or "")
    right = re.sub(r"\s+", "", shopmine_name or "")
    if not left or not right:
        return False
    if "*" not in left:
        return left == right
    if len(left) != len(right):
        return False
    return all(a == "*" or a == b for a, b in zip(left, right))


# --------------------------------------------------------------------------
# 페이지 열기 / 로그인
# --------------------------------------------------------------------------

def _looks_like_login_page(page: Page) -> bool:
    """로그인 페이지로 튕겼는지. 서버가 302로 보내주므로 주소만 보면 바로 알 수 있다."""
    if LOGIN_HOST not in page.url:
        return False
    return page.locator("input[type='password']").count() > 0


def _looks_like_bot_check(page: Page) -> bool:
    try:
        body_text = page.inner_text("body")
    except Exception:
        return False
    return any(p in body_text for p in BOT_CHECK_PATTERNS)


def _auto_login(page: Page) -> bool:
    """AUCTION_ID/AUCTION_PW로 완전 자동 로그인한다 (사용자 명시 요청).

    SSG/더현대/NS홈쇼핑/11번가 어댑터와 동일한 패턴 - 옥션도 자동 클릭 로그인이
    캡차 등에 막히지 않는 것을 확인했다.
    """
    login_id = os.environ.get("AUCTION_ID")
    login_pw = os.environ.get("AUCTION_PW")
    if not login_id or not login_pw:
        raise BlockedError(
            "옥션 로그인이 필요하지만 AUCTION_ID/AUCTION_PW 환경변수가 설정되어 있지 않습니다. .env에 추가해주세요."
        )

    page.fill(LOGIN_ID_SELECTOR, login_id)
    page.fill(LOGIN_PW_SELECTOR, login_pw)
    page.click(LOGIN_BUTTON_SELECTOR)

    elapsed_ms = 0
    while elapsed_ms < LOGIN_WAIT_TIMEOUT_MS:
        if not _looks_like_login_page(page):
            return True
        page.wait_for_timeout(500)
        elapsed_ms += 500
    return False


def _goto_logged_in(page: Page, url: str, expect_selector: str | None = None) -> None:
    """url로 이동한다. 로그인 페이지로 튕기면 자동 로그인하고 다시 이동한다.

    expect_selector는 "이동에 성공했다면 화면에 있어야 하는 것"이다. 로그인
    리다이렉트는 서버가 302로 보내주므로 보통 주소만 보면 즉시 알 수 있지만,
    자바스크립트로 뒤늦게 튕기는 경우까지 놓치지 않도록 기대한 내용이 없을
    때만 잠깐 기다렸다 다시 확인한다 (정상 경로에서는 기다리지 않는다).
    """
    page.goto(url, wait_until="domcontentloaded")
    if not _looks_like_login_page(page):
        if expect_selector is None or page.locator(expect_selector).count() > 0:
            return
        page.wait_for_timeout(1500)
        if not _looks_like_login_page(page):
            return

    common.safe_print("[auction] 로그인 세션이 없어 자동 로그인을 시도합니다.")
    if not _auto_login(page):
        raise BlockedError("옥션 자동 로그인 후에도 로그인 페이지에서 벗어나지 못했습니다.")
    page.goto(url, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        raise BlockedError("옥션 로그인 후에도 여전히 로그인 페이지입니다.")


def _open_logged_in(context: BrowserContext, url: str, expect_selector: str | None = None) -> Page:
    browser_mod.block_heavy_resources(context)
    page = context.new_page()
    try:
        _goto_logged_in(page, url, expect_selector)
        return page
    except Exception:
        page.close()
        raise


# --------------------------------------------------------------------------
# 주문내역 목록
# --------------------------------------------------------------------------


@dataclass
class ListedOrder:
    order_no: str
    order_date: str
    option: str
    product_name: str
    status: str
    has_tracking: bool


# 한 번 훑은 주문내역을 같은 실행 안에서 재사용한다. 주문이 여러 건이면
# 매번 목록을 다시 읽게 되는데, 그 목록은 실행 중에 바뀌지 않는다.
# BrowserContext는 오케스트레이터가 공급사별로 하나씩 만들어 실행이 끝날 때
# 닫으므로, 그 수명 동안만 유효한 캐시다.
_ORDER_LIST_CACHE: dict[int, list[ListedOrder]] = {}
_RECIPIENT_CACHE: dict[tuple[int, str], str] = {}


def _within_period(order_date: str, oldest_allowed: date) -> bool:
    """주문일("2026-08-24")이 조회기간 안인지. 날짜를 못 읽으면 버리지 않는다."""
    try:
        parsed = date.fromisoformat(order_date)
    except ValueError:
        return True
    return parsed >= oldest_allowed


def _load_order_list(context: BrowserContext) -> list[ListedOrder]:
    """주문내역조회를 최근 1개월치만 읽는다."""
    cached = _ORDER_LIST_CACHE.get(id(context))
    if cached is not None:
        return cached

    oldest_allowed = date.today() - timedelta(days=LIST_PERIOD_DAYS)
    page = _open_logged_in(context, ORDER_LIST_URL, expect_selector=ORDER_ROW_SELECTOR)
    try:
        if _looks_like_bot_check(page):
            raise BlockedError(
                "옥션 봇 확인(Cloudflare) 화면이 떴습니다. 브라우저에서 직접 통과한 뒤 다시 실행해주세요."
            )

        # "더보기" AJAX가 조회기간으로 쓰는 값을 1개월로 바꿔둔다 (기본값 "3M").
        page.evaluate(
            "([selector, term]) => { const el = document.querySelector(selector); if (el) el.value = term; }",
            [SEARCH_TERM_SELECTOR, SEARCH_TERM_ONE_MONTH],
        )

        rows = page.evaluate(PARSE_ORDER_ROWS_JS)
        for _ in range(MAX_MORE_CLICKS):
            # 이미 1개월보다 오래된 주문까지 나왔으면 더 볼 필요가 없다.
            if rows and not _within_period(rows[-1]["orderDate"], oldest_allowed):
                break
            more = page.locator(MORE_BAR_SELECTOR)
            if more.count() == 0 or not more.first.is_visible():
                break
            before = len(rows)
            try:
                more.first.click(timeout=3000)
                # 고정 대기 대신, 주문 행이 실제로 늘어날 때까지만 기다린다.
                page.wait_for_function(
                    "count => document.querySelectorAll(\"tr[id^='tr']\").length > count",
                    arg=before,
                    timeout=MORE_WAIT_TIMEOUT_MS,
                )
            except Exception:
                break  # 더 가져올 게 없으면 행이 늘지 않아 여기로 온다
            rows = page.evaluate(PARSE_ORDER_ROWS_JS)

        orders = [
            ListedOrder(
                order_no=row["orderNo"],
                order_date=row["orderDate"],
                option=row["option"],
                product_name=row["productName"],
                status=row["status"],
                has_tracking=bool(row["hasTracking"]),
            )
            for row in rows
            if _within_period(row["orderDate"], oldest_allowed)
        ]
        if not orders:
            raise ParseError(
                f"옥션 주문내역조회에서 최근 {LIST_PERIOD_DAYS}일 안의 주문을 하나도 읽지 못했습니다."
            )
        _ORDER_LIST_CACHE[id(context)] = orders
        common.safe_print(f"[auction] 최근 1개월 주문내역 {len(orders)}건을 읽었습니다.")
        return orders
    finally:
        page.close()


def _read_recipient(page: Page, context: BrowserContext, order_no: str) -> str:
    """주문상세 레이어의 배송지정보에서 "받으시는 분"을 읽는다 (페이지는 재사용한다)."""
    cache_key = (id(context), order_no)
    if cache_key in _RECIPIENT_CACHE:
        return _RECIPIENT_CACHE[cache_key]

    _goto_logged_in(
        page, ORDER_DETAIL_URL.format(order_no=order_no), expect_selector=DETAIL_TABLE_SELECTOR
    )
    recipient = page.evaluate(READ_RECIPIENT_JS) or ""
    _RECIPIENT_CACHE[cache_key] = recipient
    return recipient


def _find_order(
    context: BrowserContext, order_option: str | None, recipient_name: str | None
) -> ListedOrder:
    """주문옵션(+수령인)으로 어느 주문인지 찾는다."""
    if not order_option and not recipient_name:
        raise ParseError(
            "옥션은 상품URL에 주문번호가 없어 주문옵션이나 수령인으로 찾아야 하는데, 둘 다 비어 있습니다. "
            "샵마인 내보내기에 '주문옵션'/'수령인' 컬럼을 포함해주세요."
        )

    orders = _load_order_list(context)
    scored = [(option_score(order_option, order.option), order) for order in orders]
    candidates = sorted(
        [(score, order) for score, order in scored if score > 0], key=lambda x: -x[0]
    )

    if not candidates:
        raise ParseError(
            f"주문옵션 {order_option!r}과 조금이라도 맞는 주문을 최근 1개월 주문내역({len(orders)}건)에서 "
            "찾지 못했습니다 - 조회기간(1개월)을 벗어났거나 다른 계정의 주문일 수 있습니다."
        )

    if recipient_name:
        # 사용자 요청: 옵션이 같은 주문이 여러 건이라 옵션만으로는 확정할 수 없으므로
        # (실제로 "RBK / 260 / 40,000원 / 1개" 같은 옵션이 5건 넘게 반복된다)
        # 점수가 높은 순서대로 주문상세를 열어 수령인이 같은 주문을 고른다.
        # 후보마다 페이지를 새로 만들지 않고 하나를 재사용한다 (속도).
        browser_mod.block_heavy_resources(context)
        page = context.new_page()
        try:
            for _, order in candidates[:MAX_RECIPIENT_LOOKUPS]:
                if recipient_matches(_read_recipient(page, context, order.order_no), recipient_name):
                    return order
        finally:
            page.close()
        raise ParseError(
            f"주문옵션 {order_option!r}으로 찾은 후보 {min(len(candidates), MAX_RECIPIENT_LOOKUPS)}건 중 "
            f"수령인이 {recipient_name!r}인 주문이 없습니다 - 옥션에서 직접 확인해주세요."
        )

    best_score = candidates[0][0]
    tied = [order for score, order in candidates if score == best_score]
    if len(tied) > 1:
        raise ParseError(
            f"주문옵션 {order_option!r}에 똑같이 들어맞는 주문이 {len(tied)}건이라 어느 주문인지 확정할 수 없습니다 "
            f"(주문번호: {', '.join(o.order_no for o in tied[:5])}). "
            "샵마인 내보내기에 '수령인' 컬럼을 포함하면 자동으로 구분됩니다."
        )
    return tied[0]


# --------------------------------------------------------------------------
# 송장번호 조회
# --------------------------------------------------------------------------


def extract_order_no(product_url: str) -> str | None:
    """상품URL에 주문번호가 들어있으면 꺼낸다 (없으면 None - 목록에서 찾아야 한다).

    샵마인 내보내기에는 보통 목록 페이지 URL이 들어있지만, 주문상세/배송조회
    주소를 직접 넣어두면 목록을 훑지 않고 바로 조회할 수 있다.
    """
    qs = parse_qs(urlparse(product_url).query)
    for key in ("order_no", "orderNo", "orderno", "OrderNo"):
        values = qs.get(key)
        if values and values[0].strip().isdigit():
            return values[0].strip()
    return None


def _read_shipping_info(page: Page) -> dict | None:
    """배송조회 페이지의 <script id="__NEXT_DATA__">에서 shippingInfo를 꺼낸다.

    서버에서 렌더링되어 오는 값이라 페이지가 뜨는 즉시 읽을 수 있다.
    """
    locator = page.locator(NEXT_DATA_SELECTOR)
    if locator.count() == 0:
        return None
    try:
        raw = locator.first.text_content() or ""
        data = json.loads(raw)
    except Exception:
        return None
    info = data.get("props", {}).get("pageProps", {}).get("initialState", {}).get("shippingInfo")
    return info if isinstance(info, dict) else None


def _parse_delivery_text(page: Page) -> tuple[str, str] | None:
    """__NEXT_DATA__를 못 읽었을 때의 대체 경로 - "CJ대한통운 501707423071" 텍스트."""
    locator = page.locator(DELIVERY_TEXT_SELECTOR)
    if locator.count() == 0:
        return None
    try:
        text = locator.first.inner_text().strip()
    except Exception:
        return None
    match = re.match(r"^(.*?)\s+([0-9][0-9\-]{7,})$", text)
    if not match:
        return None
    return match.group(2).replace("-", ""), match.group(1).strip()


def _fetch_tracking(context: BrowserContext, order_no: str) -> TrackingResult:
    page = _open_logged_in(
        context, TRACE_URL.format(order_no=order_no), expect_selector=NEXT_DATA_SELECTOR
    )
    try:
        info = _read_shipping_info(page)
        if info is None:
            # 봇 확인 화면이면 __NEXT_DATA__도 대체 경로도 없다 - 그대로 두면
            # '아직 미발급'(스킵)으로 잘못 기록되므로 먼저 가려낸다.
            if _looks_like_bot_check(page):
                raise BlockedError(
                    "옥션 봇 확인 화면이 떴습니다 (배송조회). 브라우저에서 직접 통과한 뒤 다시 실행해주세요."
                )
            fallback = _parse_delivery_text(page)
            if fallback is None:
                raise TrackingNotAvailableYet(
                    f"배송조회 화면에 아직 송장번호가 없습니다 (주문번호={order_no})."
                )
            tracking_no, raw_courier = fallback
            return TrackingResult(tracking_no=tracking_no, courier=common.normalize_courier(raw_courier))

        # 엉뚱한 주문의 송장을 가져오지 않았는지 검증한다.
        trace_order_no = str(info.get("orderNo") or "").strip()
        if trace_order_no and trace_order_no != order_no:
            raise ParseError(
                f"배송조회 화면의 주문번호({trace_order_no})가 조회하려던 주문번호({order_no})와 다릅니다."
            )

        invoice_numbers = info.get("invoiceNo") or []
        if isinstance(invoice_numbers, str):
            invoice_numbers = [invoice_numbers]
        distinct = {re.sub(r"[^0-9]", "", str(n)) for n in invoice_numbers if str(n).strip()}
        distinct.discard("")
        if not distinct:
            raise TrackingNotAvailableYet(
                f"아직 송장번호가 발급되지 않았습니다 (주문번호={order_no})."
            )
        if len(distinct) > 1:
            raise ParseError(
                f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={order_no}) - "
                "상품별로 나눠 배송된 것으로 보입니다."
            )

        # shippingCompany는 "롯데택배                    "처럼 뒤에 공백이 붙어 오는 경우가 있다.
        raw_courier = str(info.get("shippingCompany") or "").strip()
        courier = common.normalize_courier(raw_courier) if raw_courier else DEFAULT_COURIER
        return TrackingResult(tracking_no=distinct.pop(), courier=courier)
    finally:
        page.close()


def _tracking_for_listed_order(context: BrowserContext, order: ListedOrder) -> TrackingResult:
    if not order.has_tracking:
        # 주문상태를 정확히 읽을 수 있으니 취소/품절 판정을 먼저 한다.
        raise_if_cancelled(order.status, order.order_no)
        if any(pattern in order.status for pattern in NOT_YET_STATUSES):
            raise TrackingNotAvailableYet(
                f"아직 발송 전입니다 (주문번호={order.order_no}, 주문상태={order.status})."
            )
        raise ParseError(
            f"배송조회를 할 수 없는 주문입니다 (주문번호={order.order_no}, 주문상태={order.status})."
        )
    return _fetch_tracking(context, order.order_no)


def get_tracking(
    context: BrowserContext,
    product_url: str,
    headless: bool = True,
    order_option: str | None = None,
    recipient_name: str | None = None,
) -> TrackingResult:
    order_no = extract_order_no(product_url)
    if order_no is not None:
        return _fetch_tracking(context, order_no)

    # 상품URL에 주문번호가 없으면 주문내역 목록에서 찾는다. 그 목록에 주문일이
    # 같이 들어있어서, 화면을 따로 읽지 않고 그 값을 그대로 결과에 실어준다
    # (오래된 주문을 결과에 따로 모으는 데 쓴다).
    order = _find_order(context, order_option, recipient_name)
    return attach_order_date(
        order_date_mod.parse(order.order_date),
        lambda: _tracking_for_listed_order(context, order),
    )
