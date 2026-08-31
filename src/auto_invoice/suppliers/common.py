"""공급사 어댑터 15곳이 글자까지 똑같이 쓰던 코드를 모아둔 곳.

한 사이트에만 해당하는 것(로그인 페이지 판별, 화면 파싱)은 각 어댑터에
그대로 둔다. 여기 있는 것은 '한 곳만 고치면 전부에 반영돼야 하는' 값들이다 -
택배사 표기 정규화가 15벌로 흩어져 있으면 새 표기를 추가할 때 몇 곳을
빠뜨리게 되고, 그 사이트만 조용히 다른 이름으로 송장을 올리게 된다.
"""

from __future__ import annotations

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
    for keyword, canonical in COURIER_NORMALIZATION:
        if keyword in raw:
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
    그 함수가 내부에서 poll_ms만큼 대기하기 때문에 여기서 따로 자지 않는다 -
    poll_ms는 그 대기 시간과 맞춰야 타임아웃이 실제 시간과 어긋나지 않는다.
    """
    show_page_to_human(page)
    elapsed_ms = 0
    while elapsed_ms < timeout_ms:
        if not is_login_page():
            return True
        elapsed_ms += poll_ms
    return False
