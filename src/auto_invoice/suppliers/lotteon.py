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
- **주문목록 API로 성공까지 답하기 (2026-09-02 실측).** 마이롯데 주문내역
  화면이 부르는 것은 세 가지다:
    POST pbf.lotteon.com/order/v1/mylotte/getOrderList  {pageNo, prdStrtDt,
         prdEndDt, searchPdNmBrdNmText:"", odInfwRteCd:"LTON"} -> dataList[]
         15건/페이지. 항목마다 odNo/odSeq/procSeq, **invcNo(송장번호)**,
         odAccpDttm(주문일시), dvBgtCnts("9/7(월) 이내 도착확률 90%"),
         sitmNm(옵션), pdNm(상품명). 오늘 성공한 13건의 송장이 상세 화면
         값과 전부 같았다.
    POST pbf.lotteon.com/order/v2/fo/ui/states {orderReqList:[{odNo,odSeq,
         procSeq}], withDvInfo:"N"} -> data[].stateText.title (상품준비중/
         출고지시/배송중 ...) - 목록 카드의 상태 글자가 이것이다. 한 번에
         여러 건을 물을 수 있다.
    GET  www.lotteon.com/p/delivery/deliverysearch/search?odNo=&odSeq=&invcNo=
         &procSeq=  - [배송조회] 모달이 여는 페이지. 서버가 그려주는 HTML 안의
         JSON에 dvcNm("롯데택배")이 있다. 목록 항목에는 택배사명이 없어서
         (dvMnsCd는 전부 "DPCL") 발송 건마다 이 페이지를 한 번 받는다.
  위의 Imperva 건(getOrderDetail을 페이지 안에서 연달아 fetch하니 999)이 있어
  요청 수를 **사람이 화면을 볼 때와 같게** 둔다 - 목록 페이지 수는 [더보기]
  횟수와 같고, 상태 조회는 한 번, 배송조회는 발송 건마다 한 번(모달을 여는
  것과 같다). 세 호출 모두 context.request로 보낸다(브라우저 쿠키를 그대로
  쓴다). 어느 하나라도 200이 아니면 예전의 화면 [더보기] 방식으로 돌아간다.
  실측: 목록 7페이지 4.8초 + 상태 0.4초 + 배송조회 0.3~0.5초/건. 화면 방식은
  목록만 17.8초였고 성공 건은 상세를 따로 열었다(2.5초/건).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from .. import rate_limit
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
# 주의: "결제완료"는 주문 상태가 아니라 결제정보 칸의 배지(.paymentStep
# .statusBadge)라서 **결제한 주문이면 취소된 것까지 전부** 갖고 있다. 그래서
# 이 판정은 송장이 없고 취소 표시도 없다는 걸 확인한 뒤에만 한다.
NOT_YET_PATTERNS = ["상품준비중", "배송준비중", "결제완료"]

# 주문상세에서 취소를 알아보는 자리 (2026-09-04 실측, 취소완료 주문 1건과 정상
# 주문 1건 비교). 화면 전체 텍스트로는 못 가린다 - 왼쪽 메뉴의 "취소/교환/반품
# 내역"과 정상 주문의 [취소하기] 버튼에도 '취소'가 들어 있어서다.
#   - 상태 버튼 자리(.orderStatusInfoButtons): 취소된 주문은 "취소현황",
#     아직 살아 있는 주문은 "취소하기".
#   - 결제정보 배지(.paymentStep .statusBadge): 취소된 결제에 "결제취소"가 붙는다
#     (정상 주문은 "결제완료"만).
_DETAIL_STATUS_JS = """() => ({
  buttons: [...document.querySelectorAll('.orderStatusInfoButtons')].map(el => el.innerText.trim()),
  badges: [...document.querySelectorAll('.paymentStep .statusBadge')].map(el => el.innerText.trim()),
})"""
DETAIL_CANCELLED_BUTTON = "취소현황"
DETAIL_CANCELLED_BADGE = "결제취소"
DETAIL_ALIVE_BUTTON = "취소하기"

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
# 아래 두 값은 '얼마나 기다리나'가 아니라 '최악의 경우 얼마까지만'이다 - 카드가
# 실제로 늘어나는 순간 바로 끝낸다. 예전에는 이만큼을 무조건 잤고, 그게 이
# 사이트에서 가장 큰 낭비였다(실측: 목록 훑기 39.9초 중 첫 렌더 8초 중 6.5초,
# [더보기] 14번 35초 중 22.6초가 그냥 자는 시간이었다).
LIST_RENDER_WAIT_MS = 8000   # 목록이 그려질 때까지 (SPA라 goto만으로는 비어 있다)
LIST_MORE_WAIT_MS = 2500     # [더보기] 누르고 다음 15건이 붙을 때까지
LIST_MORE_MAX_CLICKS = 30    # 무한정 누르지 않는다 (15건씩 -> 최대 450건)
# 목록 카드 하나가 주문 하나다. 몇 장 그려졌는지로 렌더가 끝났는지를 판단한다.
_CARD_COUNT_JS = "() => document.querySelectorAll('.orderGroupWrap').length"
# 목록을 훑는 값이 상세를 여는 것보다 싼 최소 건수. 몇 건 안 되면 그냥 상세를 연다.
LIST_PREFETCH_MIN_ORDERS = 5

# 주문목록 API (맨 위 docstring). 화면 방식과 요청 수를 같게 유지한다.
LIST_API_URL = "https://pbf.lotteon.com/order/v1/mylotte/getOrderList"
STATES_API_URL = "https://pbf.lotteon.com/order/v2/fo/ui/states"
TRACE_PAGE_URL = ("https://www.lotteon.com/p/delivery/deliverysearch/search"
                  "?odNo={od_no}&odSeq={od_seq}&invcNo={invc_no}&procSeq={proc_seq}")
LIST_API_MAX_PAGES = 30        # 15건씩 -> 최대 450건 (안전 상한). 보통은 아래 주문일 기준으로 훨씬 먼저 멈춘다.
LIST_API_LOOKBACK_DAYS = 45    # 발송대상에 남아 있는 주문은 이보다 오래되지 않는다
TRACE_GAP_SEC = (0.2, 0.4)     # 배송조회 페이지를 연달아 받을 때 사이 간격 (페이지 자체는 0.05초, 실측)
API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "referer": "https://www.lotteon.com/",
}
# 배송조회 페이지 HTML 안의 JSON은 따옴표가 여러 겹 이스케이프돼 있다
# ( dvcNm\\\":\\\"롯데택배 ). 백슬래시 개수에 상관없이 잡는다.
COURIER_NAME_PATTERN = re.compile(r'dvcNm\\*"\s*:\s*\\*"([^"\\]+)')

# 이 상태로 적힌 주문은 송장번호가 아직 없다 - 상세를 열어도 '미발급'만 나온다.
# 2026-08-31 실측으로 확인한 것만 넣었다(상품준비중 2/2, 출고지시 3/3이 상세에서
# 미발급). 여기 없는 상태는 예전처럼 상세를 열어 확인한다.
# 아직 발송 전이라 송장이 없는 게 정상인 상태 글자. ui/states가 상태 대신
# "09/09 도착예정"처럼 예정일 문구를 주는 주문이 있다(2026-09-03 실측, 미발송
# 7건 중 6건) - 이걸 '모르는 상태'로 보면 상세를 열러 가서 건당 1초 + 요청
# 간격이 붙는다. 송장이 이미 있는 주문은 이 판정에 오지 않으므로(아래
# _raise_if_listed_settled) 예정일 문구를 미발급으로 봐도 안전하다.
LIST_NOT_YET_STATUSES = ("상품준비중", "배송준비중", "결제완료", "입금대기",
                         "주문접수", "출고지시", "도착예정", "발송예정")

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


def _wait_for_more_cards(page, more_than: int, timeout_ms: int) -> bool:
    """목록 카드가 more_than장보다 많아질 때까지만 기다린다 (안 늘면 False).

    첫 렌더(more_than=0)와 [더보기] 뒤를 같은 함수로 본다 - 둘 다 '카드가
    늘어났는가'가 판단 기준이라, 시계를 보고 자던 것을 이걸로 대신한다.
    """
    try:
        page.wait_for_function(
            "n => document.querySelectorAll('.orderGroupWrap').length > n",
            arg=more_than, timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001 - 끝내 안 늘면 판단은 호출한 쪽이 한다
        return False


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문들을 주문내역 목록에서 미리 훑어둔다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 여기서 읽어둔
    것은 get_tracking이 상세를 열기 전에 본다 - '아직 안 나간 주문'이면
    상세를 아예 열지 않고, 송장번호까지 읽었으면 성공도 여기서 답한다.

    먼저 주문목록 API로 읽고(맨 위 docstring), 거부되면 예전의 화면
    [더보기] 방식으로 읽는다. 둘 다 실패하면 아무것도 읽지 않은 것과 같아서,
    모든 주문이 예전처럼 상세를 여는 경로로 간다. 그래서 여기서는 어떤
    예외도 밖으로 내보내지 않는다.
    """
    wanted = set()
    for order in orders:
        try:
            wanted.add(extract_od_no(order.product_url))
        except ParseError:
            continue  # 이런 주문은 어차피 상세 경로에서 같은 이유로 실패한다
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return

    found = None
    try:
        found = _prefetch_via_api(context, wanted)
    except Exception as e:  # noqa: BLE001 - API가 안 되면 화면 방식으로
        common.safe_print(f"[lotteon] 주문목록 API를 읽지 못해 화면 목록으로 대신합니다 ({e}).")
    if found is None:
        found = _prefetch_via_ui(context, wanted)
    if found is None:
        return
    _listed_orders[id(context)] = found
    shipped = sum(1 for card in found.values() if any(it.get("invcNo") for it in card.get("items") or []))
    common.safe_print(
        f"[lotteon] 주문내역 목록에서 {len(found)}/{len(wanted)}건을 미리 확인했습니다"
        f" (송장까지 읽은 건 {shipped}건).")


# API 한 번이 잠깐 실패했다고 목록 전체를 화면 방식으로 물리면, 화면 카드는
# 지난 예정일을 숨기고 상태 글자도 비어 있는 경우가 있어 예정 문구가 통째로
# 빠진다 (2026-09-04 09:25 실행: 롯데온 5건의 '출고/도착예정'이 빈칸이었고,
# 화면 목록으로 다시 읽으니 그 기록과 똑같았다). 그래서 한 번은 다시 묻는다.
API_RETRY_GAP_SEC = 1.0


def _post_json(context: BrowserContext, url: str, payload: dict) -> dict | None:
    """API 한 번 (잠깐 실패하면 한 번 더). 그래도 안 되면 None - 호출한 쪽이 화면 방식으로 물러난다."""
    name = url.rsplit("/", 1)[-1]
    for attempt in (1, 2):
        try:
            response = context.request.post(url, data=json.dumps(payload), headers=API_HEADERS)
            if response.status == 200:
                return response.json()
            problem = f"응답이 {response.status}입니다"
        except Exception as e:  # noqa: BLE001 - 네트워크 오류도 한 번은 다시 시도한다
            problem = f"요청이 실패했습니다 ({e})"
        if attempt == 1:
            common.safe_print(f"[lotteon] {name} {problem} - 잠시 후 한 번 더 시도합니다.")
            common.sleep(API_RETRY_GAP_SEC)
    common.safe_print(f"[lotteon] {name} {problem}.")
    return None


def _prefetch_via_api(context: BrowserContext, wanted: set[str]) -> dict[str, dict] | None:
    """주문목록 API로 wanted 주문들의 상태·송장·택배사를 읽는다 (docstring)."""
    today = date.today()
    # 주문번호(odNo) 앞 8자리가 주문일(YYYYMMDD)이고 목록은 최신순이다. 한
    # 페이지의 주문이 전부 찾는 주문 중 가장 오래된 것보다 앞서면 더 넘겨봐야
    # 없다 - 예전엔 한 건이라도 못 찾으면 상한(12페이지)까지 다 훑었다(7초).
    oldest_wanted = min((od[:8] for od in wanted if od[:8].isdigit()), default="")
    rows_by_od: dict[str, list[dict]] = {}
    for page_no in range(1, LIST_API_MAX_PAGES + 1):
        data = _post_json(context, LIST_API_URL, {
            "pageNo": page_no,
            "prdStrtDt": f"{today - timedelta(days=LIST_API_LOOKBACK_DAYS):%Y%m%d}",
            "prdEndDt": f"{today:%Y%m%d}",
            "searchPdNmBrdNmText": "",
            "odInfwRteCd": "LTON",
        })
        if data is None:
            return None
        rows = data.get("dataList") or []
        if not rows:
            break  # 목록의 끝
        for row in rows:
            od_no = str(row.get("odNo") or "")
            if od_no in wanted:
                rows_by_od.setdefault(od_no, []).append(row)
        if not (wanted - rows_by_od.keys()):
            break
        dates = [str(r.get("odAccpDttm") or "")[:8] for r in rows]
        dates = [d for d in dates if d.isdigit()]
        if oldest_wanted and dates and max(dates) < oldest_wanted:
            break  # 이 페이지부터는 찾는 주문보다 오래된 것뿐이다
    if not rows_by_od:
        return {}

    # 상태 글자(상품준비중/출고지시/배송중 ...)는 별도 API - 한 번에 다 묻는다.
    req = [{"odNo": r["odNo"], "odSeq": int(r["odSeq"]), "procSeq": int(r["procSeq"])}
           for rows in rows_by_od.values() for r in rows]
    states = _post_json(context, STATES_API_URL, {"orderReqList": req, "withDvInfo": "N"})
    if states is None:
        return None
    title_by_key: dict[tuple[str, str, str], str] = {}
    for d in states.get("data") or []:
        title = ((d.get("stateText") or {}).get("title") or "").strip()
        title_by_key[(str(d.get("odNo")), str(d.get("odSeq")), str(d.get("procSeq")))] = title

    found: dict[str, dict] = {}
    for od_no, rows in rows_by_od.items():
        items = []
        for r in rows:
            items.append({
                "odSeq": str(r.get("odSeq")), "procSeq": str(r.get("procSeq")),
                "invcNo": re.sub(r"[^0-9]", "", str(r.get("invcNo") or "")) or None,
                "option": r.get("sitmNm") or r.get("itmNm") or "",
                "name": r.get("pdNm") or "",
                "status": title_by_key.get((od_no, str(r.get("odSeq")), str(r.get("procSeq"))), ""),
                "eta": (r.get("dvBgtCnts") or "").strip(),
                "courier": None,
            })
        accepted = str(rows[0].get("odAccpDttm") or "")
        found[od_no] = {
            # 화면 카드와 같은 모양(date/statuses/etas)으로 둬서 판정 함수를 공유한다.
            "date": f"{accepted[:4]}.{accepted[4:6]}.{accepted[6:8]}" if len(accepted) >= 8 else "",
            "statuses": [it["status"] for it in items if it["status"]],
            "etas": [it["eta"] for it in items if it["eta"]],
            "items": items,
        }

    # 택배사명은 발송 건마다 배송조회 페이지에서 읽는다 (같은 송장은 한 번만).
    courier_by_invc: dict[str, str] = {}
    for od_no, card in found.items():
        for it in card["items"]:
            invc = it["invcNo"]
            if not invc:
                continue
            if invc not in courier_by_invc:
                if courier_by_invc:
                    time_sleep = rate_limit.request_gap(*TRACE_GAP_SEC)
                    common.sleep(time_sleep)
                courier_by_invc[invc] = _fetch_courier_name(context, od_no, it)
            it["courier"] = courier_by_invc[invc]
    return found


def _fetch_courier_name(context: BrowserContext, od_no: str, item: dict) -> str | None:
    """배송조회 페이지 HTML에서 택배사명. 못 읽으면 None - 그 주문은 상세로 간다."""
    try:
        response = context.request.get(TRACE_PAGE_URL.format(
            od_no=od_no, od_seq=item["odSeq"], invc_no=item["invcNo"], proc_seq=item["procSeq"]))
        if response.status != 200:
            return None
        match = COURIER_NAME_PATTERN.search(response.text())
    except Exception:  # noqa: BLE001 - 택배사명 하나 때문에 목록 전체를 버리지 않는다
        return None
    if not match:
        return None
    return common.normalize_courier(match.group(1).strip())


def _prefetch_via_ui(context: BrowserContext, wanted: set[str]) -> dict[str, dict] | None:
    """예전 방식 - 마이롯데 주문내역 화면을 [더보기]로 훑는다 (API 폴백)."""
    page = context.new_page()

    # [더보기] 한 번에 실제 데이터 요청(getOrderList)은 하나인데, 카드마다
    # **몸통까지 똑같은** 라벨 조회(ui/code·ui/info)가 15쌍씩 따라온다
    # (2026-09-01 실측: 첫 렌더 34건, 클릭마다 30건). 첫 응답을 받아두고
    # 반복 요청은 그걸로 대신 채워 왕복을 줄인다 - 클릭당 대기 중앙값
    # 0.9~1.3초 -> 0.7~0.8초 실측, 카드의 상태/예정 값은 캐시를 써도
    # 같았다(102장 비교). 뭐든 실패하면 원래대로 흘려보낸다.
    label_cache: dict[tuple[str, str | None], str] = {}

    def _serve_label_from_cache(route) -> None:
        try:
            key = (route.request.url, route.request.post_data)
            body = label_cache.get(key)
            if body is not None:
                route.fulfill(status=200, content_type="application/json", body=body)
                return
            response = route.fetch()
            label_cache[key] = response.text()
            route.fulfill(response=response)
        except Exception:  # noqa: BLE001 - 캐시는 거들 뿐, 실패하면 원래 경로로
            with contextlib.suppress(Exception):
                route.continue_()

    try:
        page.route("**/order/fo/ui/code", _serve_label_from_cache)
        page.route("**/order/fo/ui/info", _serve_label_from_cache)
        page.goto(ORDER_LIST_URL, wait_until="domcontentloaded")
        _wait_for_more_cards(page, 0, LIST_RENDER_WAIT_MS)
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
            before = page.evaluate(_CARD_COUNT_JS)
            more.click()
            if not _wait_for_more_cards(page, before, LIST_MORE_WAIT_MS):
                break  # 눌러도 더 안 붙으면 목록의 끝이다

        return {od_no: card for od_no, card in seen.items() if od_no in wanted}
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 그냥 예전처럼 상세를 연다
        common.safe_print(f"[lotteon] 주문내역 목록을 읽지 못해 주문마다 상세를 엽니다 ({e}).")
        return None
    finally:
        page.close()


def _listed_order_date(text: str) -> date | None:
    """목록의 '2026.08.31' 표기."""
    try:
        return datetime.strptime(text.strip(), "%Y.%m.%d").date()
    except (ValueError, AttributeError):
        return None


def _listed_note(card: dict) -> str | None:
    """목록 카드의 상태 글자와 예정 문구로 엑셀용 예정 문구를 만든다.

    줄마다 **따로** 파싱해서 합친다. 한 줄로 이어 붙이면("09/09 도착예정 9/7(월)
    이내 발송예정") eta.from_text가 라벨 뒤를 먼저 보는 규칙 때문에 '도착예정'
    뒤의 9/7을 도착예정일로 잘못 읽었다 (2026-09-03 실측).
    """
    found: list[str] = []
    for text in (card.get("statuses") or []) + (card.get("etas") or []):
        note = eta_mod.from_text(text)
        if not note:
            continue
        for item in note.split(" / "):
            if item not in found:
                found.append(item)
    return " / ".join(found) or None


def _raise_if_listed_settled(card: dict, od_no: str) -> None:
    """목록에 적힌 상태만으로 결론이 나면 상세를 열지 않고 여기서 끝낸다.

    결론이 안 나면(모르는 상태, 오래된 주문) 그냥 돌아가고, 호출한 쪽이
    예전처럼 주문상세를 연다.
    """
    statuses = [s for s in card.get("statuses") or [] if s]
    if not statuses:
        return
    order_date = _listed_order_date(card.get("date") or "")
    note = _listed_note(card)

    # 취소/품절은 기다려도 송장이 안 나온다. 주문상태를 정확히 읽을 수 있는
    # 공급사는 NOT_YET 판정보다 먼저 본다(base.raise_if_cancelled 규칙).
    # 롯데온 주문상세에는 이 표시가 안 나와서, 목록을 봐야만 알 수 있다.
    # 목록의 한 주문이 여러 줄로 뜨기도 한다. '취소' 줄과 '준비중' 줄이 같이
    # 있으면 준비 쪽이 이겨 미발급으로 넘어간다 (base.raise_if_cancelled_any).
    try:
        raise_if_cancelled_any(statuses, od_no)
    except (OrderCancelled, TrackingNotAvailableYet) as e:
        e.order_date, e.delivery_note = order_date, note
        e.sent_request = False  # 미리 읽어둔 목록으로 답했다 - 새 요청 없음
        raise

    if any(it.get("invcNo") for it in card.get("items") or []):
        return  # 송장이 이미 있다 - _answer_from_listed가 답한다 (예정일 문구를 미발급으로 오판하지 않게)
    if not all(any(k in s for k in LIST_NOT_YET_STATUSES) for s in statuses):
        return  # 모르는 상태가 섞여 있으면 상세로 확인한다

    # 주문한 지 오래된 건은 상세까지 열어 예정 문구를 읽는다 - 사람이 '왜 아직
    # 안 나갔나'를 판단할 때 쓰는 값이라(report.stale_entries), 목록에 없는
    # 문구까지 챙겨야 한다. 주문일을 못 읽었으면 오래된 건인지 알 수 없으므로
    # 마찬가지로 상세를 연다.
    #
    # 단, 그 문구를 **목록에서 이미 읽었으면** 상세를 열 이유가 없다. 롯데온은
    # 목록 카드에도 "9/3(목) 이내 도착확률 91%"를 적어두고, 상세에 있는 것도
    # 같은 문구다(2026-09-01 실측). 2026-09-01 실행 기준 롯데온 스킵 33건 중
    # 12건이 '오래된 주문'이라 이 자리에서 상세로 갔다 - 건당 1.2초씩이다.
    if note is None and (order_date is None or order_date_mod.is_stale(order_date)):
        return

    error = TrackingNotAvailableYet(
        f"아직 송장번호가 발급되지 않았습니다 (odNo={od_no}, 주문내역 '{statuses[0]}')."
    )
    error.order_date, error.delivery_note = order_date, note
    error.sent_request = False  # 미리 읽어둔 목록으로 답했다 - 새 요청 없음
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


def _click_tracking_button(page) -> bool:
    """배송상세조회 버튼을 누른다. 실제로 눌렀으면 True.

    버튼을 못 찾아도 예외로 바로 끊지 않는다 - 일부 주문은 버튼 없이
    페이지에 바로 송장번호가 보이는 경우가 있어, 호출한 쪽의 텍스트 스캔에
    맡긴다. 그래서 '눌렀는지'를 돌려준다 - 누르지도 않았는데 모달이 그려질
    때까지 기다리면 그만큼 그냥 버리는 시간이다.
    """
    for text in TRACKING_BUTTON_TEXTS:
        loc = page.get_by_text(text, exact=False)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


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


def _answer_from_listed(card: dict, od_no: str, order_option: str | None) -> TrackingResult | None:
    """목록 API가 송장번호와 택배사명까지 읽어뒀으면 상세를 열지 않고 답한다.

    None이면 결론을 못 낸 것이다(송장이 없거나, 여러 송장인데 옵션으로 못
    고르거나, 택배사명을 못 읽음) - 호출한 쪽이 예전처럼 상세를 연다. 여러
    송장인 주문은 상세 경로가 같은 규칙(옵션 매칭 -> 사람 확인)으로 처리한다.
    """
    shipped = [it for it in card.get("items") or [] if it.get("invcNo")]
    if not shipped:
        return None
    tracking_nos = {it["invcNo"] for it in shipped}
    if len(tracking_nos) == 1:
        chosen = shipped[0]
    else:
        target = normalize_option(order_option) if order_option else ""
        matched = [it for it in shipped
                   if target and target in normalize_option(f"{it.get('name', '')} {it.get('option', '')}")]
        if len({it["invcNo"] for it in matched}) != 1:
            return None
        chosen = matched[0]
    if not chosen.get("courier"):
        return None
    result = TrackingResult(tracking_no=chosen["invcNo"], courier=chosen["courier"])
    result.order_date = _listed_order_date(card.get("date") or "")
    result.delivery_note = _listed_note(card)
    result.sent_request = False  # 미리 읽어둔 목록으로 답했다 - 새 요청 없음
    return result


def _raise_if_detail_cancelled(page, od_no: str) -> None:
    """주문상세의 상태 버튼/결제 배지로 취소된 주문이면 OrderCancelled를 던진다.

    [취소하기] 버튼이 아직 있으면(일부만 취소된 주문) 살아 있는 상품이 남은
    것이라 취소로 보지 않는다 - base.raise_if_cancelled의 '준비가 이긴다'
    규칙과 같은 뜻이다.
    """
    try:
        found = page.evaluate(_DETAIL_STATUS_JS) or {}
    except Exception:  # noqa: BLE001 - 상태 요소를 못 읽으면 예전 판정으로 넘어간다
        return
    buttons = [t for t in found.get("buttons") or [] if t]
    badges = [t for t in found.get("badges") or [] if t]
    if any(DETAIL_ALIVE_BUTTON in t for t in buttons):
        return
    marker = next((t for t in buttons if DETAIL_CANCELLED_BUTTON in t), None)         or next((t for t in badges if DETAIL_CANCELLED_BADGE in t), None)
    if marker:
        raise OrderCancelled(
            f"주문상세에 '{marker}' 표시가 있습니다 (odNo={od_no}) - 취소/품절 주문인지 확인해주세요."
        )


def _scrape_tracking_from_page(page, od_no: str, order_option: str | None = None) -> TrackingResult:
    # 버튼을 눌렀으면 송장번호가 화면에 뜰 때까지만 기다린다 - 예전에는 여기서
    # 무조건 1.5초를 잤는데, 실측(실주문 3건) 0.06~0.17초면 떠서 그 차이가
    # 그대로 낭비였다. 끝내 안 뜨면 예전과 같은 1.5초를 채우고 넘어간다.
    if _click_tracking_button(page):
        body_text = common.wait_for_match(
            page, lambda: page.inner_text("body"), TRACKING_NO_PATTERN)
    else:
        body_text = page.inner_text("body")

    tracking_matches = list(TRACKING_NO_PATTERN.finditer(body_text))
    if not tracking_matches:
        # 취소를 먼저 본다. 미발급 판정의 "결제완료"는 취소된 주문에도 있어서
        # (결제정보 배지) 순서를 바꾸면 취소 건이 매번 '미발급'으로 넘어간다 -
        # 2026-09-04 취소완료 주문이 그렇게 스킵에 묻혔다.
        _raise_if_detail_cancelled(page, od_no)
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
        answered = _answer_from_listed(listed, od_no, order_option)
        if answered is not None:
            return answered

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
