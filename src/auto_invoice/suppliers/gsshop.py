"""GS SHOP(GSSHOP) 공급사 어댑터.

리버스엔지니어링 결과:
- 주문상세(배송현황) 팝업 URL: https://<호스트>/ord/dlvcursta/popup/ordDtl.gs?ordNo=<주문번호>&ecOrdTypCd=<S 등>
  (샵마인 엑셀의 "상품URL" 컬럼에 이 팝업 URL이 그대로 들어있는 것으로 확인했다.)
  호스트는 with.gsshop.com과 www.gsshop.com 두 가지로 들어오는데, 경로/응답
  구조가 완전히 같고 로그인 쿠키도 .gsshop.com 스코프라 서로 공유된다 -
  들어온 URL의 호스트를 그대로 따라가면 된다.
- GSSHOP_ID/GSSHOP_PW 환경변수가 있으면 세션 만료 시 사람 개입 없이 완전 자동으로
  재로그인한다. 다만 **로그인용 크롬 프로필에 구글 로그인이 남아 있어야** 한다
  (최초 1회, scripts/setup_gsshop_login_profile.py). 그 이유를 포함해 이 어댑터가
  다른 어댑터와 구조가 다른 점들(2026-08-28~29 실측):
    * **비밀번호 칸은 page.fill()로 채우면 사이트가 빈 칸으로 인식한다.** 값은
      DOM에도 jQuery val()에도 정상으로 들어가지만, 로그인 버튼을 누르면
      "비밀번호를 입력해주세요."가 뜨고 제출 자체가 안 된다(아이디 칸은 fill로도
      통과한다). 실제 키 입력(press_sequentially)으로 채우면 정상 진행된다 -
      이 폼의 커스텀 입력 컴포넌트가 키 이벤트로 입력 상태를 추적하는 것으로
      보인다. 그래서 이 어댑터만 타이핑 방식으로 채운다.
    * 로그인 폼에 **reCAPTCHA Enterprise**가 걸려 있다(reCaptchaFlg=true,
      sitekey 6LeYTRgs...). 로그인 버튼을 누르면 먼저
      POST /cust/cert/reCAPTCHA/createAssessment.gs 로 점수를 평가받는데,
      login.min.js를 읽어보면 응답 처리가 이렇다:
          "pass" -> encToken을 폼에 심고 그대로 제출
          "need" -> encToken을 심고 **v2 체크박스 위젯을 띄운다**. 체크박스를
                    통과시키면 위젯 콜백이 로그인까지 알아서 제출한다.
      오래 "자동화 브라우저는 항상 need를 받는다"고 접어뒀던 곳인데,
      2026-08-29에 **pass를 받는 조건을 찾았다**. 갈린 것은 두 가지다:
        (1) 크롬을 직접 실행하고 CDP로 붙을 것(real_chrome_cdp_context).
        (2) **그 프로필이 구글에 로그인되어 있을 것.**
      (2)가 결정적이었다. 브라우저 신호(navigator.webdriver=false, Runtime.enable
      누출 없음)를 다 맞춰도, 구글 계정이 없는 프로필은 3번 시도해 3번 다 need를
      받았다. 구글 로그인을 한 뒤에는 GSSHOP 쿠키를 지우고 3번 다시 시도해
      3번 다 pass였다. 사람이 평소 크롬으로 로그인하면 체크박스가 안 뜨는 것도
      같은 이유로 보인다(평소 크롬은 구글에 로그인되어 있다).
      그 구글 로그인은 최초 1회 사람이 해야 한다 -
      `scripts/setup_gsshop_login_profile.py`가 자동화를 안 붙인 평범한 크롬
      창을 띄워준다(구글은 자동화가 붙은 브라우저의 로그인을 막는다).
      평소 크롬의 쿠키를 복사해 오는 방법은 크롬 127+의 앱 바운드 암호화
      때문에 불가능하다(복사한 프로필에서는 복호화가 안 된다 - 실측).
    * 그래도 need가 오면(구글 로그인이 만료됐다든지) 예전처럼 창을 열어둔 채
      사람이 체크박스를 통과시키기를 기다린다 - 반자동 경로를 안전망으로 남겨뒀다.
    * 로그인 창은 browser.real_chrome_cdp_context()로 띄운다(우리가 직접 실행한
      크롬에 CDP로 붙는 방식). 진짜 크롬이라 예전에 쓰던 UA 우회(headless UA로는
      로그인 페이지가 941바이트 에러 페이지로 온다)가 더 이상 필요 없다. 조회는 원래 컨텍스트에서 headless로 이어간다 -
      GSSHOP은 다른 브라우저에서 만든 쿠키를 그대로 받아들인다.
  GSSHOP_PW를 비워두면 예전처럼 아이디만 자동 입력하고 사람이 직접 로그인한다.
  로그인 폼 셀렉터: 아이디 "#id", 비밀번호 "#passwd", 버튼 "#btnLogin".
- 로그인이 안 되어 있으면 /cust/login/login.gs?returnurl=... 로 리다이렉트되는데,
  이 returnurl은 원래 요청한 팝업 URL이 아니라 항상 홈(index.gs)으로 고정되어
  있었다(롯데온/지마켓과 다른 점) - 그래서 로그인 완료 후에도 자동으로 원래
  페이지로 돌아오지 않으므로, 다른 어댑터와 동일하게 로그인 감지 후 항상
  product_url로 명시적으로 다시 이동한다. 로그인 폼 셀렉터: 아이디 "#id",
  비밀번호 "#passwd".
- 로그인된 상태로 이 팝업 페이지를 열면 <script type="application/json"
  id="entry-data"> 안에 주문 전체가 JSON으로 그대로 들어있다(화면 렌더링과
  별개로 이미 응답에 포함되어 있음 - API 호출이나 버튼 클릭이 전혀 필요
  없다). ordItemList[] 각 항목의 invNo(송장번호)/dlvsCoCd(택배사 코드)/
  ordItemStExposNm(진행상태 텍스트)/exposAttrPrdNm(옵션)/exposPrdNm(상품명)/
  hopeDlvYn("E"면 새벽배송이라 아직 조회 불가)를 그대로 쓴다.
- dlvsCoCd는 "HD" 같은 내부 코드라 사람이 읽을 수 있는 택배사명이 아니다.
  화면의 "배송현황조회" 링크(data-action="dlvTrace")를 실제로 클릭하면
  /ord/dlvcursta/popup/dlvTrace.gs?ordNo=<주문번호>&ordItemId=<상품ID> 팝업이
  새 창으로 뜨는데, 이 페이지에 "택배업체  <정식명칭> 대표번호 : ..." 형태로
  실제 택배사명이 렌더링되어 있다(dlvsCoCd="HD" 확인 사례: 롯데택배). 이
  URL은 코드만 알면 그대로 다시 열 수 있어(팝업 클릭을 흉내낼 필요 없이)
  직접 이동해서 택배사명만 이 페이지에서 읽어온다 - 송장번호는 이미
  entry-data에서 얻은 값을 그대로 쓴다(더 신뢰할 수 있는 구조화된 값).
  이 페이지는 서버가 그려서 내려주므로(자바스크립트 필요 없음) 화면을 열지
  않고 context.request.get()으로 HTML만 받아 <th>택배업체</th> 다음 칸을
  읽는다 - 화면을 열고 1초 자던 예전 방식(1.5초)이 0.2~0.4초로 준다
  (2026-09-02 실측). 그리고 **같은 코드는 같은 택배사**이므로 코드별로 이름을
  기억해두고, 한 실행 안에서 같은 코드가 다시 나오면 요청 없이 답한다
  (실측 코드: HD=롯데택배, DH=CJ대한통운).

- **주문목록 한 번으로 여러 건 답하기 (prepare_batch, 2026-09-02 실측).**
  /ord/dlvcursta/ordList.gs?pageIdx=N 도 주문상세와 똑같이 <script
  id="entry-data">에 JSON을 심어서 내려준다(ordList[] 각 주문의 ordItemList[]).
  이 항목이 상세 팝업의 ordItemList 항목과 **필드까지 같다** - invNo /
  dlvsCoCd / ordItemStExposNm / hopeDlvYn / exposAttrPrdNm / exposPrdNm /
  ordDt / dlvGuideStr / dlvGuideDt 전부 (배송준비중 3건·배송완료 1건·수거중
  1건 대조, 배송 관련 값은 모두 일치). 그래서 상세 팝업 대신 목록 항목을
  같은 파서(_select_item)에 넣어 성공까지 목록으로 답한다. 기본 조회 기간은
  최근 1개월, 20건씩이고(1.2~1.5초/페이지), 필요한 주문이 다 나올 때까지만
  넘긴다. 목록에 없는 주문(1개월보다 오래됨 등)만 예전처럼 상세로 간다.
  출고/도착 예정 문구는 상세 화면 텍스트 대신 항목의 dlvGuideStr+dlvGuideDt
  ("배송예정일" + "9/5(토)까지<br/>도착예정")를 같은 파서(eta.from_text)에
  넣어 상세와 같은 문구를 만든다.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .. import browser as browser_mod
from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    AdapterError,
    AlreadyInquired,
    BlockedError,
    OrderCancelled,
    ParseError,
    TrackingNotAvailableYet,
    attach_order_date,
    normalize_option,
    raise_if_cancelled,
    with_order_date,
)

load_dotenv()

LOGIN_ID_SELECTOR = "#id"
LOGIN_PW_SELECTOR = "#passwd"
LOGIN_BUTTON_SELECTOR = "#btnLogin"
# 이미지 캡차(추가 인증) 입력칸 - 평소엔 숨어 있고, 뜨면 자동 로그인은 포기한다.
CAPTCHA_INPUT_SELECTOR = "#confirmNum"
# 로그인 버튼을 누르면 먼저 호출되는 reCAPTCHA 점수 평가 API.
RECAPTCHA_ASSESS_MARKER = "/cust/cert/reCAPTCHA/createAssessment.gs"
# 이 값을 받으면 추가 인증이 필요하다는 뜻이고, 사이트가 로그인 제출을 중단한다.
RECAPTCHA_BLOCKED_RESULT = "need"

# Playwright 기본 headless UA("HeadlessChrome")로는 로그인 페이지가 에러 페이지로
# 바뀐다(위 docstring 참고). 로그인 전용 컨텍스트에만 이 UA를 쓴다.

# 같은 주문상세 팝업이 with.gsshop.com / www.gsshop.com 두 호스트로 모두
# 들어온다(경로와 응답 구조는 동일). registry는 "www." 접두사를 떼고 찾지만,
# 다른 어댑터와 표기를 맞추려고 www 형태도 같이 적어둔다.
DOMAINS = {"with.gsshop.com", "gsshop.com", "www.gsshop.com"}
SITE_KEY = "gsshop"

TRACE_PATH = "/ord/dlvcursta/popup/dlvTrace.gs?ordNo={ord_no}&ordItemId={ord_item_id}"

DEFAULT_COURIER = "택배"  # 배송현황조회 팝업에서 택배사명을 못 읽었을 때만 쓰는 기본값

# 주문상세/주문목록 페이지가 <script type="application/json" id="entry-data">에
# 심어 내려주는 JSON. 화면을 열지 않고 HTML만 받아 여기서 바로 꺼낸다.
ENTRY_DATA_PATTERN = re.compile(r'<script[^>]*id="entry-data"[^>]*>(.*?)</script>', re.S)

# 배송현황조회 팝업 HTML의 "<th>택배업체</th><td> 롯데택배 대표번호 : ..." 칸.
COURIER_CELL_PATTERN = re.compile(r"<th>\s*택배업체\s*</th>\s*<td>\s*([^<\n]+)")

# 한 실행 안에서 이미 알아낸 {dlvsCoCd: 택배사명}. 같은 코드는 같은 택배사라
# 두 번째부터는 배송현황조회 팝업을 열지 않는다.
_courier_by_code: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 주문목록 한 번으로 여러 건 답하기 (prepare_batch) - 맨 위 docstring 참고.
# ---------------------------------------------------------------------------
ORDER_LIST_PATH = "/ord/dlvcursta/ordList.gs?pageIdx={page}"
LIST_MAX_PAGES = 5            # 20건씩 -> 최대 100건. 못 덮은 주문은 상세 폴백으로.
LIST_PREFETCH_MIN_ORDERS = 2  # 1건이면 목록이나 상세나 요청 하나라 이득이 없다.

# prepare_batch가 읽어둔 {"<주문번호>:<주문유형>": 목록의 그 주문 JSON}.
# 컨텍스트(=이번 실행의 브라우저)별로 담는다. 반품(R) 주문은 원주문과 번호가
# 달라 섞이지 않지만, URL의 ecOrdTypCd까지 같이 키로 써서 확실히 가른다.
_listed_orders: dict[int, dict[str, dict]] = {}

LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 수동 로그인 대기 최대 5분
# 로그인 리다이렉트는 두 단계로 온다(2026-09-02 실측): 주문상세 -> 302 ->
# /cust/login/popup/login.gs(입력창이 하나도 없는 빈 중간 페이지) -> 자바스크립트
# -> /cust/login/login.gs(실제 폼). 첫 단계 주소도 "/cust/login/"이라 로그인
# 판정은 즉시 나는데, 그 순간에는 아직 폼이 없다 - 폼이 붙을 때까지 이만큼
# 기다린다(실측 0.5초 안에 붙는다).
LOGIN_FORM_WAIT_MS = 10 * 1000
AUTO_LOGIN_WAIT_TIMEOUT_MS = 30 * 1000  # 사람 손이 필요 없는 구간은 짧게
CHECKBOX_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 사람이 체크박스를 누르기를 기다리는 시간
# 체크박스가 의심스러울 때 구글이 띄우는 이미지 고르기 화면(평소엔 숨어 있다).
RECAPTCHA_CHALLENGE_FRAME = "iframe[src*='bframe']"

NOT_YET_PATTERNS = ["결제완료", "상품준비중", "배송준비중", "주문확인중", "입금대기"]


def extract_order_no(product_url: str) -> str:
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    values = qs.get("ordNo")
    if not values:
        raise ParseError(f"URL에서 ordNo 파라미터를 찾을 수 없습니다: {product_url}")
    return values[0]


def extract_order_type(product_url: str) -> str:
    """URL의 ecOrdTypCd (S=일반 주문, R=반품). 없으면 S로 본다."""
    values = parse_qs(urlparse(product_url).query).get("ecOrdTypCd")
    return values[0] if values else "S"


def _list_key(ord_no: str, order_type: str) -> str:
    return f"{ord_no}:{order_type}"


def _fetch_entry_data(context: BrowserContext, url: str) -> tuple[str, dict | None]:
    """화면을 열지 않고 HTML만 받아 entry-data JSON을 꺼낸다.

    (최종 주소, JSON)을 돌려준다 - 세션이 만료됐으면 최종 주소가 로그인
    주소이고 JSON은 None이다.
    """
    response = context.request.get(url)
    match = ENTRY_DATA_PATTERN.search(response.text())
    if not match:
        return response.url, None
    return response.url, json.loads(match.group(1))


def _is_login_url(url: str) -> bool:
    return "/cust/login/" in urlparse(url).path


def prepare_batch(context: BrowserContext, orders, headless: bool = True) -> None:
    """이번에 조회할 주문들을 주문목록 JSON으로 미리 통째로 읽어둔다.

    오케스트레이터가 이 공급사의 첫 조회 전에 한 번 불러준다. 실패하면(세션
    만료 포함) 아무것도 읽지 않은 것과 같아서 모든 주문이 예전처럼 상세
    경로로 간다 - 그래서 어떤 예외도 밖으로 내보내지 않는다.
    """
    wanted: dict[str, str] = {}  # 목록 키 -> 상품URL (로그인이 필요할 때 하나 쓴다)
    for order in orders:
        try:
            wanted[_list_key(extract_order_no(order.product_url),
                             extract_order_type(order.product_url))] = order.product_url
        except ParseError:
            continue  # 이런 주문은 어차피 상세 경로에서 같은 이유로 실패한다
    if len(wanted) < LIST_PREFETCH_MIN_ORDERS:
        return

    sample_url = next(iter(wanted.values()))
    origin = extract_origin(sample_url)
    try:
        found: dict[str, dict] = {}
        for page_no in range(1, LIST_MAX_PAGES + 1):
            list_url = origin + ORDER_LIST_PATH.format(page=page_no)
            final_url, entry = _fetch_entry_data(context, list_url)
            if page_no == 1 and _is_login_url(final_url):
                # 세션이 만료됐으면 여기서 한 번 로그인해둔다 - 실패하면 물러나고,
                # 상세 경로가 주문마다 다시 시도한다(그쪽은 사람 로그인까지 기다린다).
                common.safe_print("[gsshop] 로그인 세션이 없어 자동 로그인을 시도합니다 (로그인용 크롬 창이 잠깐 뜹니다).")
                if not _auto_login(context, sample_url, headless=headless):
                    common.safe_print("[gsshop] 자동 로그인이 안 돼 주문마다 상세 화면에서 다시 시도합니다.")
                    return
                common.safe_print("[gsshop] 자동 로그인 완료.")
                final_url, entry = _fetch_entry_data(context, list_url)
            listed = (entry or {}).get("ordList") or []
            if not listed:
                break  # 목록의 끝(빈 페이지) - 못 찾은 건은 상세 폴백으로
            for order in listed:
                found[_list_key(str(order.get("ordNo")), str(order.get("ecOrdTypCd") or "S"))] = order
            if not (wanted.keys() - found.keys()):
                break
        _listed_orders[id(context)] = found
        common.safe_print(
            f"[gsshop] 주문목록에서 {len(wanted.keys() & found.keys())}/{len(wanted)}건을 미리 읽었습니다.")
    except Exception as e:  # noqa: BLE001 - 목록을 못 읽으면 그냥 상세 경로로 간다
        common.safe_print(f"[gsshop] 주문목록을 읽지 못해 주문마다 상세 화면을 엽니다 ({e}).")


def _strip_tags(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "").replace("&nbsp;", " ")).strip()


def _delivery_note_of(order: dict) -> str | None:
    """목록 항목의 안내 문구("배송예정일" + "9/5(토)까지<br/>도착예정")를 상세
    화면 텍스트와 같은 파서에 넣어 같은 문구를 만든다."""
    lines = []
    for item in order.get("ordItemList") or []:
        line = _strip_tags(f"{item.get('dlvGuideStr') or ''} {item.get('dlvGuideDt') or ''}")
        if line:
            lines.append(line)
    return eta_mod.from_text("\n".join(lines)) if lines else None


def extract_origin(product_url: str) -> str:
    """배송현황조회 팝업을 주문상세와 같은 호스트에서 연다.

    with.gsshop.com 주문을 www.gsshop.com으로(또는 그 반대로) 열면 불필요한
    호스트 이동이 생기므로, 들어온 상품URL의 호스트를 그대로 따라간다.
    """
    parsed = urlparse(product_url)
    if not parsed.scheme or not parsed.netloc:
        raise ParseError(f"상품URL 형식을 해석할 수 없습니다: {product_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_login_page(page) -> bool:
    """로그인이 필요해 로그인 화면으로 넘어갔는지.

    로그인 리다이렉트는 /cust/login/popup/login.gs(빈 중간 페이지)를 거쳐
    /cust/login/login.gs(실제 폼)로 온다. Playwright 기본 headless UA일 때는
    중간 페이지에서 멈추고 그 화면은 에러 페이지다(docstring 참고). 둘 다
    "로그인이 필요하다"는 뜻이라 경로 접두사로 함께 판정한다 - 예전처럼 비밀번호
    입력창 존재까지 요구하면 폼이 없는 후자를 놓쳐서, 로그인 안내 대신 엉뚱한
    파싱 오류가 났다. 대신 폼이 필요한 쪽은 _wait_for_login_form()으로 따로
    기다린다 - 판정 직후에는 아직 중간 페이지일 수 있기 때문이다.
    """
    return common.looks_like_login_page(
        page, lambda url: "/cust/login/" in urlparse(url).path, needs_password=False)


def _wait_for_login_form(page) -> bool:
    """로그인 판정 뒤 실제 폼(아이디 입력창)이 붙을 때까지 기다린다.

    2026-09-02에 자동 로그인이 매번 "아이디 입력창을 찾지 못했습니다"로 빠지던
    원인이다 - 판정은 중간 페이지(popup/login.gs) 주소에서 바로 나는데, 폼은
    그 뒤 자바스크립트 리다이렉트로 0.5초쯤 뒤에야 나타난다. 예전에는 판정
    자체가 1.5초를 자고 시작해서 우연히 가려져 있었다(common.looks_like_login_page
    주석 참고).
    """
    try:
        page.wait_for_selector(LOGIN_ID_SELECTOR, state="attached", timeout=LOGIN_FORM_WAIT_MS)
        return True
    except Exception:
        return False


def _prefill_login_id(page) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다."""
    common.prefill_login_id(page, page.locator(LOGIN_ID_SELECTOR), os.environ.get("GSSHOP_ID"))


def _checkbox_solved(page) -> bool:
    """체크박스가 통과됐는지 - 통과하면 사이트가 reCAPTCHA_2_token에 값을 채운다."""
    try:
        return bool(page.evaluate(
            "() => typeof reCAPTCHA_2_token !== 'undefined' && !!reCAPTCHA_2_token"
        ))
    except Exception:
        return False


def _image_challenge_visible(page) -> bool:
    """구글이 체크박스만으로 안 믿고 이미지 고르기를 띄웠는지."""
    try:
        return page.frame_locator(RECAPTCHA_CHALLENGE_FRAME).locator(
            "#rc-imageselect"
        ).is_visible(timeout=1000)
    except Exception:
        return False


def _auto_login(context: BrowserContext, product_url: str, headless: bool = True) -> bool:
    """GSSHOP_ID/GSSHOP_PW로 로그인하고, 받은 쿠키를 원래 컨텍스트에 옮긴다.

    로그인은 browser.real_chrome_cdp_context()로 띄운 크롬 창에서 한다 - 우리가
    직접 실행한 크롬에 CDP로 붙고, **그 프로필이 구글에 로그인되어 있어야**
    reCAPTCHA가 pass를 준다(맨 위 docstring의 실측 참고). 조회는 원래
    컨텍스트에서 그대로 이어간다.

    사이트 동작(login.min.js 실측):
      createAssessment 응답이 "pass"  -> 그대로 로그인이 진행된다.
                          응답이 "need" -> 사이트가 v2 체크박스 위젯을 띄운다.
                          체크박스를 통과시키면 위젯 콜백(reCaptchaVerifyCallback)이
                          validateToken.gs를 거쳐 **로그인까지 알아서 제출한다** -
                          우리가 버튼을 또 누르면 안 된다.
    평소에는 pass가 나와 사람 손이 전혀 필요 없다. need가 오는 것은 프로필의
    구글 로그인이 풀렸을 때인데, 그때는 예전처럼 창을 열어둔 채 사람이 체크박스
    통과시키기를 기다린다(안전망). 구글이 체크박스만으로 안 믿고 이미지 고르기를
    띄우는 경우가 있어(실제로 확인했다) 사람 손이 클릭 한 번보다 더 갈 수 있으니,
    그럴 때는 scripts/setup_gsshop_login_profile.py로 구글 로그인을 되살리는 게 낫다.

    --headless로 돌릴 때는(사람이 안 보고 있다는 뜻) need에서 기다리지 않고
    바로 수동 경로로 넘긴다 - 아무도 없는데 5분씩 멈춰 있으면 안 되기 때문이다.

    비밀번호가 없거나 끝까지 로그인이 안 되면 False를 돌려주고 호출자가 기존
    수동 로그인 경로로 넘어간다.
    """
    login_id = os.environ.get("GSSHOP_ID")
    login_pw = os.environ.get("GSSHOP_PW")
    if not login_id or not login_pw:
        return False

    try:
        # 크롬을 직접 실행하고 CDP로 붙어야 reCAPTCHA가 pass를 준다(맨 위 docstring).
        with browser_mod.real_chrome_cdp_context(SITE_KEY) as login_context:
            return _login_in_window(
                login_context, context, product_url, headless, login_id, login_pw
            )
    except Exception as exc:
        common.safe_print(
            f"[gsshop] 자동 로그인 중 오류({type(exc).__name__}: {exc}) - 직접 로그인으로 넘어갑니다. "
            "(로그인용 크롬이 이미 다른 창으로 떠 있으면 이렇게 됩니다 - 그 창을 닫고 다시 실행해보세요.)"
        )
        return False


def _login_in_window(
    login_context,
    context: BrowserContext,
    product_url: str,
    headless: bool,
    login_id: str,
    login_pw: str,
) -> bool:
    """_auto_login이 띄운 크롬 창 안에서 실제로 로그인한다."""
    alerts: list[str] = []
    assess_results: list[str] = []
    page = login_context.pages[0] if login_context.pages else login_context.new_page()
    # 이 사이트 로그인은 PC 페이지다 - 모바일 폭으로 열면 체크박스 위젯이
    # 왼쪽으로 잘려, need가 떠서 사람이 눌러야 할 때 누르기 어렵다.
    page.set_viewport_size(browser_mod.DESKTOP_VIEWPORT)
    page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))

    def _on_response(response) -> None:
        if RECAPTCHA_ASSESS_MARKER not in response.url:
            return
        try:
            assess_results.append(str((response.json() or {}).get("result")))
        except Exception:
            return

    page.on("response", _on_response)

    # 실제 흐름 그대로 - 주문상세로 들어가서 로그인 페이지로 리다이렉트시킨다.
    page.goto(product_url, wait_until="domcontentloaded")
    if not _looks_like_login_page(page):
        # 로그인 페이지가 아니면(=이 프로필에 로그인이 남아 있으면) 쿠키만 옮기고 끝낸다.
        context.add_cookies(login_context.cookies())
        return True

    if not _wait_for_login_form(page):
        common.safe_print(
            f"[gsshop] 로그인 페이지에서 아이디 입력창을 찾지 못했습니다(주소={page.url}) "
            "- 직접 로그인으로 넘어갑니다."
        )
        return False

    # fill()로 채우면 사이트가 빈 칸으로 인식한다(docstring 참고) - 실제로 타이핑한다.
    page.locator(LOGIN_ID_SELECTOR).click()
    page.locator(LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=60)
    page.locator(LOGIN_PW_SELECTOR).click()
    page.locator(LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=60)
    page.wait_for_timeout(500)
    page.locator(LOGIN_BUTTON_SELECTOR).first.click()

    deadline_ms = AUTO_LOGIN_WAIT_TIMEOUT_MS
    elapsed_ms = 0
    asked_for_checkbox = False
    checkbox_solved = False
    announced_challenge = False

    while elapsed_ms < deadline_ms:
        # 로그인이 끝나기를 기다리는 쉼 - 예전에는 _looks_like_login_page가
        # 매번 자면서 이 역할까지 겸했다(common.looks_like_login_page 주석).
        page.wait_for_timeout(1500)
        # 로그인 페이지를 벗어났으면 성공이다. alert이 떴더라도 로그인
        # 자체는 된 경우(비밀번호 변경 안내 등)가 있어 화면을 먼저 본다.
        if not _looks_like_login_page(page):
            context.add_cookies(login_context.cookies())
            return True
        if alerts:
            common.safe_print(f"[gsshop] 로그인이 거부됐습니다: {alerts[0].strip()}")
            return False
        if _captcha_is_visible(page):
            common.safe_print("[gsshop] 로그인에 사이트 자체 보안문자가 요구돼 자동 로그인을 건너뜁니다.")
            return False

        if assess_results and assess_results[-1] == RECAPTCHA_BLOCKED_RESULT:
            if headless:
                common.safe_print(
                    "[gsshop] reCAPTCHA가 체크박스 확인을 요구합니다(result=need). "
                    "--headless로는 눌러줄 사람이 없어 수동 로그인으로 넘어갑니다. "
                    "(로그인용 프로필의 구글 로그인이 풀렸을 수 있습니다 - "
                    "scripts/setup_gsshop_login_profile.py를 다시 실행해보세요.)"
                )
                return False
            if not asked_for_checkbox:
                asked_for_checkbox = True
                deadline_ms = CHECKBOX_WAIT_TIMEOUT_MS  # 사람을 기다리는 동안은 넉넉하게
                common.safe_print(
                    "[gsshop] 아이디와 비밀번호는 넣었습니다. 뜬 크롬 창에서 "
                    "'로봇이 아닙니다' 체크박스만 눌러주세요. (로그인용 프로필의 "
                    "구글 로그인이 풀리면 이렇게 됩니다 - 다음부터 안 뜨게 하려면 "
                    "scripts/setup_gsshop_login_profile.py를 다시 실행하세요.)"
                )
                common.safe_print("[gsshop] 체크가 끝나면 로그인은 자동으로 이어서 누릅니다 (최대 5분 대기).")
            if not checkbox_solved and _checkbox_solved(page):
                # 통과시키면 위젯 콜백(reCaptchaVerifyCallback)이 validateToken을
                # 거쳐 로그인까지 알아서 제출한다 - 우리가 버튼을 또 누르면 안 된다.
                checkbox_solved = True
                common.safe_print("[gsshop] 체크박스 통과를 확인했습니다 - 로그인이 이어서 진행됩니다.")
            elif not announced_challenge and _image_challenge_visible(page):
                # 구글이 이미지 고르기를 띄운 경우. 사람이 풀면 그대로 진행되므로
                # 기다리되, 왜 클릭만으로 안 끝나는지는 알려준다.
                announced_challenge = True
                common.safe_print(
                    "[gsshop] 구글이 체크박스만으로 안 믿고 이미지 확인을 띄웠습니다. "
                    "풀기 어려우면 그냥 두세요 - 시간이 지나면 기존 수동 경로로 넘어갑니다."
                )

        elapsed_ms += 1500

    if asked_for_checkbox:
        common.safe_print("[gsshop] 체크박스 대기 시간(5분)이 지났습니다 - 수동 로그인으로 넘어갑니다.")
    else:
        common.safe_print(
            f"[gsshop] 로그인이 {AUTO_LOGIN_WAIT_TIMEOUT_MS // 1000}초 안에 끝나지 않아 수동 로그인으로 "
            f"넘어갑니다 (reCAPTCHA 평가={assess_results or '응답 없음'}, 주소={page.url})."
        )
    return False


def _captcha_is_visible(page) -> bool:
    """이미지 캡차 입력칸이 실제로 화면에 떴는지 (평소에는 숨어 있다)."""
    locator = page.locator(CAPTCHA_INPUT_SELECTOR)
    if locator.count() == 0:
        return False
    try:
        return locator.first.is_visible()
    except Exception:
        return False


def _wait_for_manual_login(page) -> bool:
    return common.wait_for_manual_login(
        page, lambda: _looks_like_login_page(page), LOGIN_WAIT_TIMEOUT_MS)


def _read_entry_data(page, ord_no: str) -> dict:
    locator = page.locator("#entry-data")
    if locator.count() == 0:
        raise ParseError(f"주문 정보(entry-data)를 찾지 못했습니다 (주문번호={ord_no}).")
    try:
        return json.loads(locator.inner_text())
    except Exception as e:
        raise ParseError(f"주문 정보(entry-data) 파싱에 실패했습니다 (주문번호={ord_no}): {e}") from e


def _find_item_by_order_option(shipped: list[dict], order_option: str | None) -> dict | None:
    """샵마인 엑셀의 "주문옵션" 값으로 상품을 정확히 짚을 수 있으면 그걸
    쓴다. 매칭이 0개(표기가 서로 안 맞음)거나 2개 이상(애매함)이면 None을
    반환해서 호출자가 기존 방식(개수 비교)으로 넘어가게 한다."""
    if len(shipped) <= 1 or not order_option:
        return None
    target = normalize_option(order_option)
    if not target:
        return None
    matched = [
        item
        for item in shipped
        if target in normalize_option(item.get("exposAttrPrdNm")) or target in normalize_option(item.get("exposPrdNm"))
    ]
    return matched[0] if len(matched) == 1 else None


def _select_item(entry: dict, ord_no: str, order_option: str | None) -> dict:
    items = entry.get("ordItemList") or []
    if not items:
        raise ParseError(f"주문 응답에 상품 정보가 없습니다 (주문번호={ord_no}).")

    # 새벽배송(hopeDlvYn="E")은 아직 조회 시점이 아니면 송장이 있어도 화면에서
    # 막아둔다(사이트 자체가 "배송현황조회 가능 시간이 아닙니다"라고 안내함) -
    # invNo가 비어있는 경우와 동일하게 미발급으로 취급한다.
    shipped = [it for it in items if it.get("invNo") and it.get("hopeDlvYn") != "E"]
    if not shipped:
        status_text = items[0].get("ordItemStExposNm", "알 수 없음")
        # 주문상태를 정확히 읽을 수 있으니 취소/품절 판정을 먼저 한다.
        raise_if_cancelled(status_text, ord_no)
        raise TrackingNotAvailableYet(f"아직 송장번호가 발급되지 않았습니다 (주문번호={ord_no}, 상태={status_text}).")

    matched = _find_item_by_order_option(shipped, order_option)
    if matched is not None:
        return matched

    tracking_nos = {it["invNo"] for it in shipped}
    if len(tracking_nos) > 1 and len(items) != len(tracking_nos):
        # 상품 개수와 송장 개수가 다르면 아직 일부만 발송된 것인지, 일부 상품이
        # 같은 박스로 묶여 나간 것인지 텍스트만으로는 구분할 수 없다 - 안전하게
        # 사람이 확인하게 한다 (무신사 어댑터와 동일한 규칙).
        raise ParseError(f"한 주문에 서로 다른 송장번호가 여러 개 있습니다 (주문번호={ord_no}) - 상품별로 나눠 배송된 것으로 보입니다.")

    return shipped[0]


def _courier_name(context: BrowserContext, origin: str, ord_no: str, item: dict) -> tuple[str, bool]:
    """상품의 택배사명과, 그걸 알아내려고 요청을 보냈는지.

    dlvsCoCd가 이미 아는 코드면 요청 없이 답한다. 아니면 배송현황조회 팝업
    HTML만 받아 "택배업체" 칸을 읽고 코드별로 기억해둔다(맨 위 docstring).
    """
    code = str(item.get("dlvsCoCd") or "").strip()
    if code and code in _courier_by_code:
        return _courier_by_code[code], False

    trace_url = origin + TRACE_PATH.format(ord_no=ord_no, ord_item_id=item["ordItemId"])
    html = context.request.get(trace_url).text()
    match = COURIER_CELL_PATTERN.search(html)
    if not match:
        return DEFAULT_COURIER, True
    courier = common.normalize_courier(match.group(1).strip())
    if code:
        _courier_by_code[code] = courier
    return courier, True


def _answer_from_list(context: BrowserContext, order: dict, ord_no: str, origin: str,
                      order_option: str | None) -> TrackingResult:
    """미리 읽어둔 주문목록 항목으로 결론을 낸다 - 상세 팝업과 같은 구조라
    같은 파서(_select_item)를 쓴다. 택배사명 때문에 요청을 보낸 경우만
    sent_request=True로 표시해서 오케스트레이터가 간격을 지키게 한다."""
    sent_request = False

    def fetch() -> TrackingResult:
        nonlocal sent_request
        item = _select_item(order, ord_no, order_option)
        tracking_no = re.sub(r"[^0-9]", "", str(item["invNo"]))
        courier, sent_request = _courier_name(context, origin, ord_no, item)
        return TrackingResult(tracking_no=tracking_no, courier=courier)

    try:
        result = attach_order_date(order_date_mod.from_json(order), fetch,
                                   delivery_note=_delivery_note_of(order))
    except AdapterError as e:
        e.sent_request = sent_request
        raise
    result.sent_request = sent_request
    return result


def get_tracking(
    context: BrowserContext, product_url: str, headless: bool = True, order_option: str | None = None
) -> TrackingResult:
    ord_no = extract_order_no(product_url)
    origin = extract_origin(product_url)

    # 주문목록에서 이미 통째로 읽어둔 주문이면 상세 팝업을 열지 않고 여기서
    # 끝낸다 (prepare_batch). 목록에 없던 주문(1개월보다 오래됨 등)만 상세로.
    listed = _listed_orders.get(id(context), {}).get(
        _list_key(ord_no, extract_order_type(product_url)))
    if listed is not None:
        return _answer_from_list(context, listed, ord_no, origin, order_option)

    page = context.new_page()
    try:
        page.goto(product_url, wait_until="domcontentloaded")

        if _looks_like_login_page(page):
            # 로그인은 자체 크롬 창에서 하므로 headless 실행 중에도 시도할 수 있다.
            # (다만 체크박스가 뜨면 --headless에서는 눌러줄 사람이 없어 포기한다.)
            if _auto_login(context, product_url, headless=headless):
                common.safe_print("[gsshop] 로그인 세션이 없어 새로 로그인했습니다.")
            elif headless:
                raise BlockedError(
                    "GSSHOP 로그인이 필요합니다. 이 사이트는 로그인 폼의 reCAPTCHA가 체크박스 "
                    "확인을 요구할 때가 있어 사람이 한 번 눌러줘야 하니, --headless 없이 실행하거나 "
                    "scripts/import_chrome_session.py로 크롬 세션을 가져와주세요."
                )
            else:
                _wait_for_login_form(page)  # 중간 페이지라면 폼이 올 때까지 잠깐
                _prefill_login_id(page)
                common.safe_print("[gsshop] 아이디는 자동으로 입력했습니다. 뜬 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                common.safe_print("[gsshop] 로그인이 완료되면 자동으로 이어서 진행합니다 (최대 5분 대기).")
                if not _wait_for_manual_login(page):
                    raise BlockedError("로그인 대기 시간(5분)이 지났습니다. 로그인 후 다시 실행해주세요.")
            # GSSHOP은 로그인 후 원래 페이지가 아니라 항상 홈으로 이동하므로
            # 명시적으로 다시 이동해야 한다.
            page.goto(product_url, wait_until="domcontentloaded")
            if _looks_like_login_page(page):
                raise BlockedError("로그인 후에도 여전히 로그인 페이지입니다.")

        entry = _read_entry_data(page, ord_no)

        def fetch() -> TrackingResult:
            item = _select_item(entry, ord_no, order_option)
            tracking_no = re.sub(r"[^0-9]", "", str(item["invNo"]))
            courier, _ = _courier_name(context, origin, ord_no, item)
            return TrackingResult(tracking_no=tracking_no, courier=courier)

        # 주문일은 화면 텍스트에서 라벨을 찾는 것보다 entry-data(JSON)에서
        # 읽는 쪽이 정확하다 (오래된 주문을 결과에 따로 모으는 데 쓴다).
        return with_order_date(page, fetch, data=entry)
    finally:
        page.close()


# --------------------------------------------------------------------------
# 1:1 상담 남기기 (post_inquiry) - 2026-09-08 실측
# --------------------------------------------------------------------------
# 주문일이 이틀 지나도록 안 나간 주문에 "<주문번호> <수령인> 배송 언제 시작하나요?"를
# 남긴다(inquiry.py). 사람이 누르는 순서는 주문상세 팝업 -> 상품명 -> [마이쇼핑] ->
# 왼쪽 메뉴 [1:1 상담하기] -> 상담 유형 [배송 문의] -> 문의상품 선택 칸(내용) ->
# [문의하기] -> 마이쇼핑 [나의 상담 내역] > [PC 상담]에서 확인이다. 실측한 구조:
#   [1:1 상담하기] = 고정 주소 www.gsshop.com/cust/custCent/main.gs (주문번호가 주소에
#     안 붙는다 - SSG처럼). 그래서 주문상세 화면은 열 필요가 없고, 취소/품절 여부와
#     상품 정보만 주문상세 JSON(entry-data, 화면 없이 HTML만 받는다)으로 본다.
#   상담 유형 드롭다운 = <a id="query_3" onclick="func_select('01')">배송 문의</a> ->
#     숨은 폼 #board 의 #prsnConslTypCd 에 코드가 들어간다 (01=배송 문의, 03=상품 문의,
#     23=결제/주문취소, 06=반품/교환, 11=이벤트/적립/혜택, 21=알림/회원/기타).
#   문의상품 선택 탭 [주문내역]은 팝업(/ord/dlvcursta/popup/ordList.gs)을 띄우고, 그
#     [선택]이 opener.setPrdResult('1', 주문번호, prdCd, 상품명, 주문일시, 'PA', 이미지,
#     옵션, 정가, 판매가, ordItemNo)를 부른다 - 서버로 가는 값은 그중 ordNo/ordItemNo/
#     prdCd 뿐이고 전부 주문상세 JSON(ordItemList[].ordNo/ordItemNo/prdCd)에 있어 팝업
#     없이 같은 함수를 직접 불러 붙인다. 사람은 이 칸을 안 쓰고 내용에 주문번호를
#     적지만, 상품까지 붙여두면 상담원이 주문을 바로 본다(내용의 주문번호는 그대로).
#   내용 = textarea#email_desc (2,000자) -> [문의하기](#request-submit-button, submitForm)
#     -> confirm("등록하시겠습니까?") -> jQuery POST /cust/myshop/inqry.gs 에 #board 를
#     serialize (questCntnt, ordNo, ordItemNo, prdCd, custMailChkYn, custSmsSndYn=Y,
#     prsnConslTypCd) -> JSON {retCd:"SUCC"} -> confirm("1:1문의가 접수되었습니다 ...
#     1:1문의내역을 확인하시겠습니까?") -> 확인이면 oneConsl.gs?#EMAIL, 취소면 reload.
#     retCd가 SUCC가 아니면 alert(retMsg) 뒤 reload. #blockCustYn=Y 면 "1:1상담을
#     이용하실 수 없습니다"로 막힌다. 로그인이 없으면 이 화면이 로그인으로 넘어간다.
#     **함정:** 완료 confirm을 닫으면(확인이든 취소든) 페이지가 이동/새로고침돼 등록
#     응답 본문을 그 뒤에는 못 읽는다(첫 실등록 때 retCd=None으로 실패라고 잘못
#     판정했다). 그래서 등록 요청을 page.route로 받아 본문을 먼저 읽어둔다(_serve_post).
#   그 POST는 폼이 만드는 값이 전부라 폼 없이 바로 보낼 수 있다(_submit_via_api) -
#     네이버와 같은 직행. 거부되면(HTTP 오류·retCd가 SUCC 아님·JSON 아님) 그 사이
#     올라갔는지 오늘 자 상담내역을 본 뒤 폼을 열어 같은 순서로 남긴다(_submit_via_form).
# 상담내역 확인: [나의 상담 내역] > [PC 상담] 화면(oneConsl.gs?#EMAIL)이 부르는
#   oneConsl.gs?ajaxYn=Y&tabGbnCd=EMAIL&currPageNo=N (HTML 조각, 20건씩 최신순,
#   #totalCnt)에 줄마다 <li id="email<문의번호>"> 등록일(2026.09.08)·내용 첫 줄·상태
#   (답변대기 -> 답변완료)가 있다. 내용이 주문번호로 시작하므로
#   상세(oneConslDtl.gs?oneConslId=, 답변까지 있는 HTML 조각) 없이 주문을 맞춘다.
#   **등록 전**에 주문일 이후 같은 주문의 '배송 언제' 문의가 있으면 AlreadyInquired로
#   넘긴다(2026-09-08 실측: 사용자가 그날 직접 남긴 3471224776 최창호 등 3건이 있었다).
#   **등록 후**에는 오늘 자로 올라갔는지 확인해 완료 문구에 붙인다.
INQUIRY_FORM_URL = "https://www.gsshop.com/cust/custCent/main.gs"
INQUIRY_POST_PATH = "/cust/myshop/inqry.gs"
INQUIRY_POST_URL = "https://www.gsshop.com" + INQUIRY_POST_PATH
INQUIRY_LIST_URL = ("https://www.gsshop.com/cust/myshop/oneConsl.gs"
                    "?ajaxYn=Y&tabGbnCd=EMAIL&currPageNo={page}")
INQUIRY_LIST_MARKER = 'id="oneConsl-tab1"'
INQUIRY_TYPE_CODE = "01"             # 상담 유형 [배송 문의]
INQUIRY_TYPE_LABEL = "배송 문의"
INQUIRY_CONTENT = "#email_desc"
INQUIRY_CONTENT_MAX = 2000
INQUIRY_SUBMIT = "#request-submit-button"
INQUIRY_BLOCKED_FLAG = "#blockCustYn"
INQUIRY_LOGIN_FLAG = "#entryLoginFlg"
INQUIRY_MESSAGE = "{order_no} {name} 배송 언제 시작하나요?"
INQUIRY_SAME_MARK = "배송 언제"       # 상담내역에서 '같은 문의'로 보는 표식 (주문번호와 함께)
INQUIRY_DONE_TEXT = "1:1문의가 접수되었습니다"
INQUIRY_SUCCESS_CODE = "SUCC"
INQUIRY_STEP_WAIT_MS = 10000         # 화면 요소·등록 응답·완료 창이 오기까지 최대
INQUIRY_HISTORY_TRIES = 3            # 등록 뒤 목록에 아직 안 보이면 이만큼 다시 본다
INQUIRY_HISTORY_RETRY_GAP_SEC = 1.0
INQUIRY_HISTORY_MAX_PAGES = 5        # '이미 남겼는지' 훑는 상담내역 페이지 수 (20건씩)
INQUIRY_ROWS_PER_PAGE = 20
# 문의 화면에서 받아줄 호스트 - 화면·API·정적파일이 이 안이다. 나머지는 광고/분석
# 태그(google·airbridge·megadata·widerplanet 등)라 끊는다.
INQUIRY_ALLOWED_HOSTS = ("gsshop.com", "m-gs.kr")
INQUIRY_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
INQUIRY_ROW_PATTERN = re.compile(r'<li class="panel-box" id="email(\d+)">(.*?)</li>', re.S)
INQUIRY_ROW_DATE = re.compile(r'<p class="rdate">.*?</span>\s*(\d{4}\.\d{2}\.\d{2})', re.S)
INQUIRY_ROW_TEXT = re.compile(r'<dd class="text-nowrap">(.*?)</dd>', re.S)
INQUIRY_ROW_STATE = re.compile(r'<span class="reply-state[^"]*">\s*(.*?)\s*</span>', re.S)
INQUIRY_TOTAL_PATTERN = re.compile(r'id="totalCnt"[^>]*value="(\d+)"')
# 폼의 setPrdResult에 넘기는 주문상세 JSON 항목의 값들 (표시용 - 서버로는 ordNo/ordItemNo/prdCd만 간다).
INQUIRY_ITEM_FIELDS = ("prdCd", "exposPrdNm", "ordDtFullStr", "prdImgUrlPath",
                       "exposAttrPrdNm", "stdUprc", "lastUprc", "ordItemNo")
INQUIRY_ATTACH_JS = (
    "([ordNo, item]) => setPrdResult('1', ordNo, String(item.prdCd || ''), item.exposPrdNm || '', "
    "item.ordDtFullStr || '', 'PA', item.prdImgUrlPath || '', item.exposAttrPrdNm || '', "
    "(item.stdUprc || '') + '원', (item.lastUprc || '') + '원', String(item.ordItemNo || ''))")
INQUIRY_FILLED_JS = (
    "() => ({type: document.getElementById('prsnConslTypCd').value, "
    "ordNo: document.getElementById('ordNo').value, prdCd: document.getElementById('prdCd').value, "
    "noPrd: document.getElementById('noPrdChk').value})")

# 이 실행(컨텍스트)에서 이미 읽어둔 상담내역 {"rows": [...최신순], "pages": n, "total": n}.
# 한 배치의 주문들이 같은 목록을 보므로 주문마다 다시 받지 않는다(prepare_inquiries가 비운다).
_inquiry_rows_cache: dict[int, dict] = {}


def inquiry_message(recipient_name: str, product_url: str | None = None) -> str:
    """사용자 문구 그대로 "<주문번호> <수령인> 배송 언제 시작하나요?" (상품URL이 없으면 주문번호 없이)."""
    name = recipient_name.strip()
    if not product_url:
        return f"{name} 배송 언제 시작하나요?"
    return INQUIRY_MESSAGE.format(order_no=extract_order_no(product_url), name=name)


def prepare_inquiries(context: BrowserContext, product_urls, headless: bool = False) -> None:
    """이번에 문의할 주문들의 주문상세(취소/품절·상품)를 주문목록으로 미리 읽어두고,
    상담내역 캐시는 새 배치라 비운다. 송장조회의 prepare_batch와 같은 목록이다."""
    _inquiry_rows_cache.pop(id(context), None)
    prepare_batch(context, [SimpleNamespace(product_url=u) for u in product_urls], headless=headless)


def _abort_third_party(route) -> None:
    """문의 화면 전용 라우팅 - GSSHOP 밖 호스트와 이미지·폰트는 끊고 나머지는 보낸다."""
    request = route.request
    host = urlparse(request.url).netloc.lower()
    allowed = any(host == h or host.endswith("." + h) for h in INQUIRY_ALLOWED_HOSTS)
    if allowed and request.resource_type not in INQUIRY_BLOCKED_RESOURCE_TYPES:
        route.continue_()
    else:
        route.abort()


def _order_date_of(entry: dict, ord_no: str) -> date:
    """주문상세 JSON의 ordDt("2026.09.04")."""
    try:
        return datetime.strptime(str(entry.get("ordDt") or ""), "%Y.%m.%d").date()
    except ValueError:
        raise ParseError(
            f"주문 정보에서 주문일을 읽을 수 없습니다 (주문번호={ord_no}, ordDt={entry.get('ordDt')!r})."
        ) from None


def _order_for_inquiry(context: BrowserContext, product_url: str, ord_no: str, headless: bool) -> dict:
    """주문상세 JSON - prepare_inquiries가 읽어둔 목록 항목이면 그것을, 아니면 화면 없이
    HTML만 받아 꺼낸다. 세션이 없으면 자동 로그인 뒤 한 번 더 받는다."""
    listed = _listed_orders.get(id(context), {}).get(_list_key(ord_no, extract_order_type(product_url)))
    if listed is not None:
        return listed
    final_url, entry = _fetch_entry_data(context, product_url)
    if entry is None and _is_login_url(final_url):
        common.safe_print("[gsshop] 로그인 세션이 없어 자동 로그인을 시도합니다 (로그인용 크롬 창이 잠깐 뜹니다).")
        if not _auto_login(context, product_url, headless=headless):
            raise BlockedError("GSSHOP 로그인이 필요합니다. 송장조회를 한 번 돌려 로그인해두거나 "
                               "--headless 없이 실행해주세요.")
        common.safe_print("[gsshop] 자동 로그인 완료.")
        final_url, entry = _fetch_entry_data(context, product_url)
    if entry is None:
        raise ParseError(f"주문 정보(entry-data)를 찾지 못했습니다 (주문번호={ord_no}, 주소={final_url}).")
    return entry


def _inquiry_item(entry: dict, ord_no: str) -> dict:
    """문의에 붙일 상품 - 취소/품절이 아닌 첫 상품. 전부 취소/품절이면 OrderCancelled."""
    items = entry.get("ordItemList") or []
    if not items:
        raise ParseError(f"주문 응답에 상품 정보가 없습니다 (주문번호={ord_no}).")
    first_error: OrderCancelled | None = None
    for item in items:
        try:
            raise_if_cancelled(item.get("ordItemStExposNm"), ord_no)
        except OrderCancelled as e:
            first_error = first_error or e
            continue
        return item
    assert first_error is not None
    raise first_error


def _get_html(context: BrowserContext, url: str) -> str | None:
    """브라우저 쿠키로 GET - 로그인 화면으로 넘어가거나 실패하면 None."""
    try:
        response = context.request.get(url)
    except Exception:  # noqa: BLE001 - 통신 실패는 '못 읽음'
        return None
    if not response.ok or _is_login_url(response.url):
        return None
    return response.text()


def _parse_inquiry_rows(fragment: str) -> list[dict]:
    """상담내역 HTML 조각에서 줄마다 문의번호·등록일·내용·상태 (최신순)."""
    rows: list[dict] = []
    for m in INQUIRY_ROW_PATTERN.finditer(fragment):
        chunk = m.group(2)
        d = INQUIRY_ROW_DATE.search(chunk)
        t = INQUIRY_ROW_TEXT.search(chunk)
        s = INQUIRY_ROW_STATE.search(chunk)
        if not d or not t:
            continue
        text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t.group(1)))).strip()
        rows.append({
            "inquiry_id": m.group(1),
            "written_on": datetime.strptime(d.group(1), "%Y.%m.%d").date(),
            "text": text,
            "state": html_mod.unescape(s.group(1)).strip() if s else "",
        })
    return rows


def _fetch_inquiry_page(context: BrowserContext, page_no: int) -> tuple[list[dict], int]:
    """상담내역 한 페이지 (줄들, 전체 건수). 목록을 못 읽으면 ParseError - 모르는 채로 등록하지 않는다."""
    fragment = _get_html(context, INQUIRY_LIST_URL.format(page=page_no))
    if fragment is None or INQUIRY_LIST_MARKER not in fragment:
        raise ParseError("나의 상담 내역을 읽지 못했습니다 (로그인 세션이 없거나 화면이 바뀜).")
    total = INQUIRY_TOTAL_PATTERN.search(fragment)
    return _parse_inquiry_rows(fragment), int(total.group(1)) if total else 0


def _load_inquiry_rows(context: BrowserContext, since: date | None, *, refresh: bool = False,
                       max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> list[dict]:
    """since 이후 줄이 다 들어올 때까지 상담내역을 읽어둔 캐시(최신순). refresh면 1페이지부터 새로."""
    cache = _inquiry_rows_cache.get(id(context))
    if cache is None or refresh:
        rows, total = _fetch_inquiry_page(context, 1)
        cache = {"rows": rows, "pages": 1, "total": total}
        _inquiry_rows_cache[id(context)] = cache
    while (since is not None and cache["rows"] and cache["pages"] < max_pages
           and cache["total"] > cache["pages"] * INQUIRY_ROWS_PER_PAGE
           and cache["rows"][-1]["written_on"] >= since):
        more, _ = _fetch_inquiry_page(context, cache["pages"] + 1)
        if not more:
            break
        cache["rows"].extend(more)
        cache["pages"] += 1
    return cache["rows"]


def _describe_listed(entry: dict) -> str:
    return f"{entry.get('state') or '상태 모름'} {entry['written_on']:%Y.%m.%d} (문의번호 {entry['inquiry_id']})"


def _find_listed_inquiry(context: BrowserContext, ord_no: str, since: date | None, *,
                         refresh: bool = False, max_pages: int = INQUIRY_HISTORY_MAX_PAGES) -> dict | None:
    """상담내역에서 since 이후에 쓴, 이 주문번호의 '배송 언제' 문의를 찾는다 (사람이 직접 남긴 것 포함)."""
    for entry in _load_inquiry_rows(context, since, refresh=refresh, max_pages=max_pages):
        if since is not None and entry["written_on"] < since:
            return None   # 최신순이라 여기부터는 전부 더 오래된 것
        if ord_no in entry["text"] and INQUIRY_SAME_MARK in entry["text"]:
            return entry
    return None


def _confirm_inquiry_listed(context: BrowserContext, ord_no: str, message: str) -> str:
    """등록 뒤 상담내역을 새로 받아 오늘 자로 올라갔는지 본다. 목록이 늦게 갱신될 수 있어 몇 번 다시 본다."""
    today = date.today()
    for attempt in range(1, INQUIRY_HISTORY_TRIES + 1):
        found = _find_listed_inquiry(context, ord_no, today, refresh=True, max_pages=1)
        if found is not None:
            return _describe_listed(found)
        if attempt < INQUIRY_HISTORY_TRIES:
            common.sleep(INQUIRY_HISTORY_RETRY_GAP_SEC)
    raise ParseError(
        f"완료 응답은 받았지만 나의 상담 내역에서 확인되지 않았습니다. "
        f"다시 남기기 전에 GSSHOP 마이쇼핑 > 나의 상담 내역 > PC 상담에서 '{message}'가 있는지 직접 확인해주세요.")


def _inquiry_payload(ord_no: str, item: dict, message: str) -> dict[str, str]:
    """[문의하기]가 보내는 숨은 폼 #board 의 값 그대로."""
    return {
        "questCntnt": message[:INQUIRY_CONTENT_MAX],
        "ordNo": ord_no,
        "ordItemNo": str(item.get("ordItemNo") or ""),
        "prdCd": str(item.get("prdCd") or ""),
        "custMailChkYn": "",
        "custSmsSndYn": "Y",
        "prsnConslTypCd": INQUIRY_TYPE_CODE,
    }


def _submit_via_api(context: BrowserContext, ord_no: str, item: dict, message: str) -> str | None:
    """[문의하기]가 보내는 등록 요청을 폼 없이 바로 보낸다.

    응답이 retCd SUCC면 완료 문구, 그 밖의 무엇이든(HTTP 오류·retCd가 SUCC 아님·JSON
    아님) None을 돌려주고 호출자가 검증된 폼 경로로 넘어간다 - 등록됐는지 모호한
    채로 성공이라 하지도, 바로 실패라 하지도 않는다.
    """
    try:
        response = context.request.post(
            INQUIRY_POST_URL, form=_inquiry_payload(ord_no, item, message),
            headers={"accept": "application/json, text/javascript, */*; q=0.01",
                     "x-requested-with": "XMLHttpRequest",
                     "origin": "https://www.gsshop.com", "referer": INQUIRY_FORM_URL})
        body = response.json() if "json" in response.headers.get("content-type", "") else None
    except Exception as e:  # noqa: BLE001
        common.safe_print(f"[gsshop] 등록 요청을 바로 보내지 못했습니다({e}) - 폼으로 남깁니다.")
        return None
    if response.status == 200 and isinstance(body, dict) and body.get("retCd") == INQUIRY_SUCCESS_CODE:
        return f"등록 요청 성공(retCd {INQUIRY_SUCCESS_CODE})"
    common.safe_print(
        f"[gsshop] 바로 보낸 등록 요청이 거부됐습니다(HTTP {response.status}, {str(body)[:80]}) - 폼으로 남깁니다.")
    return None


def _prepare_inquiry_form(page, ord_no: str, item: dict, message: str) -> None:
    """1:1 상담 화면을 열어 유형·상품·내용을 채운다 - [문의하기]는 누르지 않는다."""
    page.goto(INQUIRY_FORM_URL, wait_until="domcontentloaded")
    if _looks_like_login_page(page):
        raise BlockedError("1:1 상담 화면이 로그인 화면으로 넘어갔습니다.")
    try:
        page.locator(INQUIRY_SUBMIT).wait_for(state="visible", timeout=INQUIRY_STEP_WAIT_MS)
    except PlaywrightTimeoutError:
        raise ParseError(f"1:1 상담 화면이 뜨지 않았습니다 (url={page.url}).") from None
    flags = page.evaluate(
        "([login, blocked]) => [login, blocked].map(sel => (document.querySelector(sel) || {}).value)",
        [INQUIRY_LOGIN_FLAG, INQUIRY_BLOCKED_FLAG])
    if flags[0] is not None and flags[0] != "true":
        raise BlockedError("1:1 상담 화면이 로그인 안 된 상태로 떴습니다.")
    if flags[1] == "Y":
        raise BlockedError("이 계정은 1:1 상담을 이용할 수 없다고 표시돼 있습니다 (blockCustYn=Y).")
    # 상담 유형 [배송 문의] - 드롭다운 항목의 onclick(func_select)을 그대로 부르고 표시 글자도 맞춘다.
    page.evaluate("code => func_select(code)", INQUIRY_TYPE_CODE)
    page.evaluate("label => { const el = document.getElementById('combo_t'); if (el) el.textContent = label; }",
                  INQUIRY_TYPE_LABEL)
    # 문의상품 - [주문내역] 팝업의 [선택]이 부르는 setPrdResult를 같은 인자로 부른다.
    page.evaluate(INQUIRY_ATTACH_JS, [ord_no, {k: item.get(k) for k in INQUIRY_ITEM_FIELDS}])
    page.locator(INQUIRY_CONTENT).fill(message[:INQUIRY_CONTENT_MAX])
    filled = page.evaluate(INQUIRY_FILLED_JS)
    if filled["type"] != INQUIRY_TYPE_CODE:
        raise ParseError(f"상담 유형이 [{INQUIRY_TYPE_LABEL}]로 잡히지 않았습니다 (값: {filled['type']!r}).")
    if filled["ordNo"] != ord_no or filled["prdCd"] != str(item.get("prdCd") or "") or filled["noPrd"] != "N":
        raise ParseError(f"문의상품이 이 주문으로 붙지 않았습니다 (폼: {filled}).")
    if page.locator(INQUIRY_CONTENT).input_value().strip() != message[:INQUIRY_CONTENT_MAX].strip():
        raise ParseError("문의 내용이 입력되지 않았습니다.")


def _serve_post(captured: dict):
    """등록 요청(POST inqry.gs)을 서버로 보내고 응답을 페이지에 그대로 돌려주되, 본문을 먼저 읽어둔다.

    완료 confirm을 닫으면 페이지가 바로 이동/새로고침돼 그 뒤에는 응답 본문을 못
    읽는다(맨 위 주석의 함정). 검증 스크립트는 이 함수를 바꿔 끼워 서버 없이 재본다.
    """
    def _handler(route) -> None:
        response = route.fetch()
        captured["status"] = response.status
        try:
            captured["body"] = response.json() if "json" in response.headers.get("content-type", "") else None
        except Exception:  # noqa: BLE001
            captured["body"] = None
        route.fulfill(response=response)
    return _handler


def _submit_via_form(context: BrowserContext, ord_no: str, item: dict, message: str) -> str:
    """1:1 상담 화면을 열어 채우고 [문의하기]를 눌러 완료 문구를 돌려준다.

    등록 요청의 응답(retCd SUCC)과 완료 confirm 둘 다 있어야 성공이다.
    """
    page = context.new_page()
    page.set_viewport_size(browser_mod.DESKTOP_VIEWPORT)
    page.route("**/*", _abort_third_party)
    try:
        _prepare_inquiry_form(page, ord_no, item, message)
        dialogs: list[tuple[str, str]] = []
        captured: dict = {}
        page.route(f"**{INQUIRY_POST_PATH}*", _serve_post(captured))   # 나중에 건 것이 먼저 잡는다

        def _on_dialog(dialog) -> None:
            dialogs.append((dialog.type, dialog.message))
            if INQUIRY_DONE_TEXT in dialog.message:
                dialog.dismiss()   # "문의내역을 확인하시겠습니까?" - 이동 대신 새로고침 (목록은 따로 본다)
            else:
                dialog.accept()    # "등록하시겠습니까?" 승인, 안내 alert 닫기

        page.on("dialog", _on_dialog)
        try:
            with page.expect_response(lambda r: urlparse(r.url).path == INQUIRY_POST_PATH
                                      and r.request.method == "POST",
                                      timeout=INQUIRY_STEP_WAIT_MS):
                page.locator(INQUIRY_SUBMIT).click()
        except PlaywrightTimeoutError as e:
            seen = " / ".join(f"{t}: {m}" for t, m in dialogs) or "(뜬 창 없음)"
            raise ParseError(f"[문의하기]를 눌렀는데 등록 요청이 나가지 않았습니다 ({seen}).") from e
        body = captured.get("body") if isinstance(captured.get("body"), dict) else {}
        api_ok = body.get("retCd") == INQUIRY_SUCCESS_CODE
        waited = 0
        while not any(INQUIRY_DONE_TEXT in m for _, m in dialogs) and waited < INQUIRY_STEP_WAIT_MS:
            page.wait_for_timeout(100)
            waited += 100
        done = [m for _, m in dialogs if INQUIRY_DONE_TEXT in m]
        if not done or not api_ok:
            seen = " / ".join(f"{t}: {m}" for t, m in dialogs) or "(뜬 창 없음)"
            detail = f", {body.get('retMsg')}" if body.get("retMsg") else ""
            raise ParseError(f"등록 응답 HTTP {captured.get('status')}, retCd={body.get('retCd')!r}{detail}, "
                             f"완료 문구를 받지 못했습니다 ({seen}).")
        return done[0].strip().split("\n")[0].rstrip(".")
    finally:
        page.close()


def post_inquiry(context: BrowserContext, product_url: str, recipient_name: str,
                 headless: bool = False) -> str:
    """1:1 상담(상담 유형 [배송 문의])을 남기고 완료 문구를 돌려준다.

    취소/품절 주문은 남기지 않고, 나의 상담 내역에 이 주문의 같은 문의가 주문일
    이후에 이미 있으면 AlreadyInquired로 넘긴다. 등록은 [문의하기]가 보내는 요청을
    바로 보내고(_submit_via_api), 거부되면 화면을 열어 남긴다(_submit_via_form).
    어느 쪽이든 상담내역에 오늘 자로 올라갔는지까지 확인한다. 어디서든 어긋나면
    ParseError/BlockedError - 남겼는지 불확실한 채로 성공이라 하지 않는다.
    """
    ord_no = extract_order_no(product_url)
    message = inquiry_message(recipient_name, product_url)
    entry = _order_for_inquiry(context, product_url, ord_no, headless)
    item = _inquiry_item(entry, ord_no)
    existing = _find_listed_inquiry(context, ord_no, _order_date_of(entry, ord_no))
    if existing is not None:
        raise AlreadyInquired(f"나의 상담 내역에 이미 같은 문의가 있습니다: {_describe_listed(existing)}")

    done = _submit_via_api(context, ord_no, item, message)
    if done is None:
        # 거부 응답이었어도 그 사이 올라갔을 수 있으니 폼을 열기 전에 오늘 자를 한 번 본다.
        posted = _find_listed_inquiry(context, ord_no, date.today(), refresh=True, max_pages=1)
        if posted is not None:
            return f"등록 요청은 거부 응답이었지만 상담내역에 올라감 · 나의 상담 내역: {_describe_listed(posted)}"
        done = _submit_via_form(context, ord_no, item, message)
    listed = _confirm_inquiry_listed(context, ord_no, message)
    return f"{done} · 나의 상담 내역: {listed}"
