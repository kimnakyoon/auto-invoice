"""공급사 어댑터 15곳이 글자까지 똑같이 쓰던 코드를 모아둔 곳.

한 사이트에만 해당하는 것(로그인 페이지 판별, 화면 파싱)은 각 어댑터에
그대로 둔다. 여기 있는 것은 '한 곳만 고치면 전부에 반영돼야 하는' 값들이다 -
택배사 표기 정규화가 15벌로 흩어져 있으면 새 표기를 추가할 때 몇 곳을
빠뜨리게 되고, 그 사이트만 조용히 다른 이름으로 송장을 올리게 된다.
"""

from __future__ import annotations

import time
from typing import Callable

from .. import browser as browser_mod


def safe_print(message: str) -> None:
    """GUI(pythonw)로 실행하면 콘솔이 없어 stdout이 없을 수 있다 - 그 경우 조용히 무시한다."""
    try:
        print(message)
    except Exception:  # noqa: BLE001 - 로그 한 줄 때문에 조회가 깨지면 안 된다
        pass


# 사이트마다 택배사를 다르게 적어서(CJ대한통운 / 대한통운 / CJ택배) 샵마인이
# 아는 이름으로 맞춰준다. 위에서부터 먼저 걸리는 것을 쓴다.
COURIER_NORMALIZATION = [
    ("대한통운", "CJ대한통운"),
    ("CJ", "CJ대한통운"),
    ("롯데", "롯데택배"),
    ("DELIBOX", "딜리박스"),
]


def normalize_courier(raw: str) -> str:
    """대소문자를 가리지 않고 본다 - 같은 택배사를 사이트마다 DELIBOX/Delibox
    처럼 다르게 적어서, 글자 그대로 비교하면 한쪽만 조용히 원문으로 올라간다.
    """
    upper = raw.upper()
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword.upper() in upper:
            return canonical
    return raw


def show_page_to_human(page) -> None:
    """사람에게 화면을 넘기기 직전에 부른다 - 막아둔 이미지를 다시 받게 한다.

    조회용 컨텍스트는 이미지/폰트를 받지 않는다(browser.block_heavy_resources).
    그 상태로 로그인 화면을 사람에게 넘기면 지마켓 캡차처럼 이미지로 나오는
    것을 읽을 수가 없어서, 차단을 풀고 화면을 한 번 다시 불러온다.

    차단이 이미 풀려 있으면 아무것도 하지 않는다 - 사람이 뭔가 입력하기
    시작한 뒤에 새로고침이 걸리면 입력한 것이 날아간다.
    """
    try:
        if not browser_mod.allow_heavy_resources(page.context):
            return
        page.reload(wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001 - 이미지 하나 때문에 로그인 대기를 깨지 않는다
        pass


def prefill_login_id(page, locator, login_id: str | None) -> None:
    """비밀번호는 절대 자동 입력하지 않는다 - 아이디만 채워서 타이핑을 줄인다.

    locator는 어댑터가 자기 화면에 맞게 만들어서 넘긴다(id 셀렉터를 쓰는 곳도
    있고 무신사처럼 placeholder로 찾는 곳도 있다).
    """
    show_page_to_human(page)
    if not login_id:
        return
    if locator.count() == 0:
        return
    try:
        locator.fill(login_id)
    except Exception:  # noqa: BLE001 - 못 채우면 사람이 직접 치면 된다
        pass


def wait_for_manual_login(page, is_login_page: Callable[[], bool],
                          timeout_ms: int, poll_ms: int = 1500) -> bool:
    """사람이 직접 로그인을 끝낼 때까지 화면 상태를 보며 기다린다.

    is_login_page는 각 어댑터의 _looks_like_login_page를 그대로 넘기면 된다.
    """
    show_page_to_human(page)
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        if not is_login_page():
            return True
        page.wait_for_timeout(poll_ms)
        elapsed_ms += poll_ms
    return False


# 로그인 주소가 되기를 기다리는 시간. 자바스크립트로 로그인 화면에 넘기는
# 사이트(무신사·CJ온스타일·NS홈쇼핑)만 이 값을 준다 - 아래 함수 주석 참고.
LOGIN_REDIRECT_SETTLE_MS = 1500
# 로그인 주소는 맞는데 입력창이 아직 안 그려졌을 때 기다리는 시간.
LOGIN_FORM_SETTLE_MS = 1500


def sleep(seconds: float) -> None:
    """요청 사이 간격용. 페이지 없이 API만 부르는 자리에서 쓴다."""
    if seconds > 0:
        time.sleep(seconds)


def goto_settled(page, url: str, *, retries: int = 3, settle_ms: int = 1500) -> None:
    """url로 이동하되, 진행 중인 리다이렉트 체인에 인터럽트되면 쉬었다 다시 간다.

    로그인/SSO 직후에는 사이트가 여러 단계 리다이렉트를 도는 중이라, 그때
    goto하면 Playwright가 "is interrupted by another navigation"으로 예외를
    낸다 (옥션 2026-09-01 실측: LoginThrough.aspx가 끼어들었다).
    ERR_ABORTED는 떠나려는 페이지가 자기 리다이렉트를 쏘면서 우리 goto를
    끊은 것이다 (옥션 2026-09-02 실측). 둘 다 잠깐 가라앉히고 다시 가면 된다.
    """
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:  # noqa: BLE001 - 이동이 끊긴 경우만 다시 시도한다
            if ("is interrupted by another navigation" not in str(e)
                    and "net::ERR_ABORTED" not in str(e)):
                raise
            last_error = e
            page.wait_for_timeout(settle_ms)
    raise last_error  # type: ignore[misc]


def wait_for_url(page, url_matches: Callable[[str], bool], timeout_ms: int,
                 poll_ms: int = 100) -> bool:
    """주소가 이렇게 바뀌는지 그동안만 지켜본다 (자바스크립트 리다이렉트용).

    이미 그 주소면 기다리지 않고 바로 True다. timeout_ms=0이면 지금 주소만
    본다 - 서버가 302로 넘기는 사이트는 goto가 끝난 순간 이미 끝나 있어서
    기다릴 이유가 없다.
    """
    waited_ms = 0
    while not url_matches(page.url):
        if waited_ms >= timeout_ms:
            return False
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms
    return True


def looks_like_login_page(page, url_is_login: Callable[[str], bool], *,
                          needs_password: bool = True,
                          settle_ms: int = 0,
                          form_wait_ms: int = LOGIN_FORM_SETTLE_MS,
                          poll_ms: int = 100) -> bool:
    """지금 보고 있는 화면이 로그인 화면인지 본다 (어댑터 공통).

    예전에는 어느 어댑터든 판정 전에 **무조건 1.5초를 잤다**. 리다이렉트가
    자리잡을 시간을 주려던 것인데, 주문 하나당 1.5초면 100건에 2분 반이고
    로그인이 살아 있는 평소 실행에서는 그 시간이 통째로 낭비다(실측: SSF샵
    주문 하나 1.87초 중 1.50초).

    2026-08-31에 세션 없는 브라우저로 15곳 주문상세를 열어 재보니 12곳은
    서버가 302로 넘겨서 **goto가 끝난 순간 이미 로그인 주소**였다. 그래서
    기본값은 기다리지 않는 것으로 두고, 자바스크립트로 뒤늦게 넘기는 사이트만
    settle_ms를 줘서 그동안 주소가 바뀌는지 지켜본다(바뀌는 순간 바로 끝낸다).

    반대로 '주소는 로그인인데 입력창이 아직 없는' 경우(현대몰·더현대 실측)는
    form_wait_ms 동안 기다려준다. 예전의 고정 1.5초로는 아슬아슬하게 놓칠 수
    있었고, 이쪽은 어차피 로그인이 뒤따르는 느린 경로라 조금 기다려도 된다.

    needs_password=False는 로그인 화면에 비밀번호 입력창이 없거나(GSSHOP 팝업)
    입력 중에 다시 그려져 믿을 수 없는 사이트(네이버·무신사)용이다.
    """
    if not wait_for_url(page, url_is_login, settle_ms, poll_ms=poll_ms):
        return False
    if not needs_password:
        return True
    try:
        page.wait_for_selector("input[type='password']", state="attached", timeout=form_wait_ms)
        return True
    except Exception:  # noqa: BLE001 - 입력창이 끝내 안 뜨면 로그인 화면이 아니라고 본다
        return False


# 주문상세가 그려지기를 기다리는 최대 시간 (자바스크립트로 그리는 사이트용).
RENDER_WAIT_TIMEOUT_MS = 8 * 1000

# 주문상세에 '판단 재료'(배송조회 링크나 진행중 상태 문구)가 나타나기를 기다리는
# 시간. 예전에 어댑터마다 박혀 있던 고정 1.5초를 대신하는 값이라 그보다는
# 넉넉하게 두되, 둘 다 끝내 안 나오는 화면(취소 주문 등)에서 오래 붙들리지
# 않도록 위의 8초보다는 짧게 잡는다. 실제로는 보통 이미 그려져 있어서 0.3초쯤에
# 지나간다.
ORDER_RENDER_WAIT_MS = 2000


def wait_for_text(page, needles, timeout_ms: int = RENDER_WAIT_TIMEOUT_MS,
                  poll_ms: int = 100) -> bool:
    """화면 글자에 이 말이 나타날 때까지만 기다린다 (주문상세 렌더 대기).

    롯데온·지마켓처럼 자바스크립트로 주문상세를 그리는 사이트는, 예전에는
    로그인 판정용 고정 대기(1.5초)가 렌더 대기까지 겸하고 있었다. 그 대기를
    없애면서 '무엇을 기다리는지'를 명시적으로 적는다 - 주문번호처럼 그 주문
    화면에서만 나오는 말을 넘기면 **다 그려졌다**와 **맞는 주문이다**를 한
    번에 확인할 수 있고, 고정 1.5초보다 빠르면서(실측 0.3~0.7초) 느린 날에는
    더 오래 기다려 준다.

    needles는 한 개(문자열)를 넘겨도 되고 여러 개를 넘겨도 된다 - 여러 개면
    그중 아무거나 먼저 나오는 순간 끝낸다(네이버페이처럼 주문번호를 화면에
    안 적는 사이트는 '배송조회'나 진행중 상태 문구를 표식으로 쓴다).

    끝내 안 나와도 예외를 내지 않는다(False) - 판단은 호출한 쪽이 한다.
    """
    wanted = [needles] if isinstance(needles, str) else [n for n in needles if n]
    if not wanted:
        return False
    waited_ms = 0
    while waited_ms < timeout_ms:
        try:
            text = page.inner_text("body")
            if any(n in text for n in wanted):
                return True
        except Exception:  # noqa: BLE001 - 그리는 중에는 읽기가 실패할 수 있다
            pass
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms
    return False


# 배송조회 버튼을 누른 뒤 모달/팝업/iframe이 그려지기를 기다리는 상한.
# 이 값은 '얼마나 기다려야 하나'가 아니라 '최악의 경우 얼마까지만 기다리나'다 -
# 예전에 어댑터에 박혀 있던 고정 대기와 같은 값으로 두었기 때문에, 끝내 아무것도
# 안 나오는 화면에서도 예전보다 느려지지 않는다. 실측(롯데온 실주문 3건)으로는
# 누른 뒤 0.06~0.17초면 송장번호가 떠서, 보통은 여기 근처도 가지 않는다.
MODAL_RENDER_WAIT_MS = 1500


def wait_for_match(page, read_text, pattern, timeout_ms: int = MODAL_RENDER_WAIT_MS,
                   poll_ms: int = 50) -> str:
    """이 정규식이 걸릴 때까지만 기다리고, 마지막으로 읽은 텍스트를 돌려준다.

    배송조회 버튼을 누른 뒤 '송장번호가 화면에 뜰 때까지'를 기다리는 데 쓴다.
    wait_for_text와 같은 생각(시계가 아니라 화면을 보고 정한다)인데, 두 가지가
    다르다.

      - 찾는 것이 정해진 낱말이 아니라 **송장번호 패턴**이다. 어댑터마다
        이미 그 정규식을 갖고 있으므로 그것을 그대로 넘겨 쓴다.
      - 읽을 곳이 페이지 본문만이 아니다. 지마켓은 모달이 별도 iframe이고
        롯데아이몰은 팝업 창이라, '어디서 읽을지'를 호출한 쪽이 함수로 넘긴다.
        page는 '기다리는 데'만 쓴다(아래 참고).

    쉬는 것은 반드시 page.wait_for_timeout으로 한다 - time.sleep으로 쉬면
    Playwright 동기 API의 진행 자체가 멈춰서, 기다리는 동안 화면이 그려지지
    않는다(실측: 같은 주문이 time.sleep 쪽에서는 4/4 '미발급'으로 나왔다).

    끝내 안 나와도 예외를 내지 않는다 - 마지막으로 읽은 텍스트를 그대로 주고,
    '아직 미발급인지 취소인지'는 예전처럼 호출한 쪽이 그 텍스트로 판단한다.
    """
    text = ""
    waited_ms = 0
    while True:
        try:
            text = read_text() or ""
            if pattern.search(text):
                return text
        except Exception:  # noqa: BLE001 - 그리는 중에는 읽기가 실패할 수 있다
            pass
        if waited_ms >= timeout_ms:
            return text
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms
