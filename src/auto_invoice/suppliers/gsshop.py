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

import json
import os
import re
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext

from .. import browser as browser_mod
from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult
from . import common
from .base import (
    AdapterError,
    BlockedError,
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
                # 세션이 만료됐으면 여기서 한 번 로그인해둔다 - 실패하면 조용히
                # 물러나고, 사유는 상세 경로의 로그인 시도가 주문별로 남긴다.
                if not _auto_login(context, sample_url, headless=headless):
                    return
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
        common.safe_print(f"[gsshop] 로그인 중 오류({exc}) - 직접 로그인으로 넘어갑니다.")
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
            f"[gsshop] 로그인 페이지에서 아이디 입력창을 찾지 못했습니다({page.url}) "
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
        common.safe_print("[gsshop] 로그인이 시간 안에 끝나지 않아 수동 로그인으로 넘어갑니다.")
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
