"""작업을 시작하기 전에 샵마인을 [배송중] 탭으로 옮긴다.

왜 필요한가: 2단계 이후의 모든 조작이 배송중 탭 화면을 기준으로 한다.
[송장수정모드 켜기] 좌표도, Alt+X(엑셀파일생성) / Alt+U(발송정보일괄등록)도
지금 보고 있는 탭으로 간다. 사람이 신규주문 탭을 보다가 그대로 두면
자동화가 엉뚱한 화면을 조작하게 되므로, 맨 처음에 탭부터 맞춘다.

화면 구조 (2026-08-28 실측):

    메인 창
      menuStrip1                       <- [주문관리(O)] 등 메뉴바
      MDICLIENT                        <- 탭 하나 = MDI 자식 창 하나
        '홈' / '신규주문' / '발송대기' / '배송중' / ...

즉 **탭은 진짜 창이고 제목도 진짜 창 제목이다.** 그래서 좌표를 쓸 일이 없다:

  - 지금 어느 탭인가  -> `WM_MDIGETACTIVE` (표준 메시지, 다른 프로세스에도 동작)
  - 그 탭으로 옮기기  -> `WM_MDIACTIVATE` 를 MDICLIENT 에 보낸다

탭 막대(상단 24px)를 좌표로 클릭할 수도 있지만, 탭 개수에 따라 위치가 밀리고
막대가 넘치면 스크롤까지 된다. 창 핸들로 직접 활성화하는 쪽이 정확하다.
실제로 보내보면 탭 막대의 표시도 같이 따라온다 (실측 확인).

사람이 탭 막대의 [X]로 배송중 탭을 아예 닫아버렸다면 활성화할 창 자체가
없다. 그때만 메뉴 [주문관리(O)] > [배송중(S)] 로 다시 연다. 이 메뉴는
드롭다운 안에서 니모닉(S)이 먹지 않아서(실측), connect.py 와 같은 방식으로
**반전된 항목의 세로 위치를 픽셀로 확인하면서** 방향키로 내려간다.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import tempfile
import time
from pathlib import Path

from . import winui

u = winui.u

SHIPPING_TAB = "배송중"

WM_MDIACTIVATE = 0x0222
WM_MDIGETACTIVE = 0x0229
SMTO_ABORTIFHUNG = 0x0002

VK_O = 0x4F
VK_DOWN = 0x28
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

# 드롭다운에서 선택된 항목의 배경색 (Windows 11 기본 테마) - connect.py 와 같다.
MENU_HIGHLIGHT = (181, 215, 242)

# 탭이 닫혀 있을 때 다시 여는 메뉴 경로: 탭 이름 -> (메뉴바 단축키, 드롭다운
# 안에서 그 항목이 차지하는 세로 위치(팝업 상대)). [주문관리(O)] 드롭다운은
# 항목 높이 26px, 첫 항목이 y=3 부터라 세 번째 항목([배송중])의 한가운데가 72다.
MENU_ROUTES = {SHIPPING_TAB: (VK_O, 72)}

TMP = Path(tempfile.gettempdir())


class TabError(RuntimeError):
    """원하는 탭으로 옮기지 못한 상황. 이대로 진행하면 엉뚱한 화면을 조작한다."""


def _main_window():
    wins = winui.find_windows(title_startswith="ShopMine::")
    if not wins:
        raise TabError("샵마인이 실행 중이지 않습니다.")
    return wins[0][0]


def _mdi_client(main_hwnd):
    for k in winui.children(main_hwnd):
        if winui.class_of(k) == "MDICLIENT":
            return k
    raise TabError("샵마인 창에서 탭 영역(MDICLIENT)을 찾지 못했습니다.")


def open_tabs(mdi_hwnd) -> dict[str, int]:
    """지금 열려 있는 탭들. {탭 이름: 창 핸들}."""
    out: dict[str, int] = {}
    enum = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def collect(h, _l):
        if u.GetParent(h) == mdi_hwnd:
            title = winui.ctrl_text(h)
            if title:
                out[title] = h
        return True

    u.EnumChildWindows(mdi_hwnd, enum(collect), 0)
    return out


def _active_hwnd(mdi_hwnd):
    """지금 보고 있는 탭의 창 핸들. 읽지 못하면 None.

    `SendMessage` 대신 `SendMessageTimeout` 을 쓴다 - 샵마인이 수집/연결로
    UI 스레드를 붙들고 있으면 그냥 SendMessage 는 같이 멈춘다 (connect.py 와
    같은 이유). 못 읽는 것은 '아직 확인 못함'이지 '없음'이 아니다.
    """
    res = ctypes.c_size_t(0)
    ok = u.SendMessageTimeoutW(mdi_hwnd, WM_MDIGETACTIVE, 0, 0,
                               SMTO_ABORTIFHUNG, 1500, ctypes.byref(res))
    return int(res.value) if ok and res.value else None


def current_tab(mdi_hwnd=None) -> str | None:
    """지금 보고 있는 탭 이름. 읽지 못하면 None."""
    if mdi_hwnd is None:
        mdi_hwnd = _mdi_client(_main_window())
    hwnd = _active_hwnd(mdi_hwnd)
    if hwnd is None:
        return None
    return winui.ctrl_text(hwnd) or None


def _wait_for_tab(mdi_hwnd, name, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if current_tab(mdi_hwnd) == name:
            return True
        time.sleep(0.15)
    return False


def _activate(mdi_hwnd, hwnd, name, timeout: float) -> bool:
    """이미 열려 있는 탭을 활성화한다.

    `PostMessage` 로 보낸다. `SendMessage` 는 대상이 그 메시지를 다 처리할
    때까지 돌아오지 않아서, 탭이 화면을 다시 그리는 동안 통째로 멈춘다
    (winui.press_button 과 같은 이유).
    """
    u.PostMessageW(mdi_hwnd, WM_MDIACTIVATE, hwnd, 0)
    return _wait_for_tab(mdi_hwnd, name, timeout)


# --- 탭이 닫혀 있을 때: 메뉴로 다시 열기 ----------------------------

def _menu_popup(main_hwnd):
    """메뉴바 드롭다운 팝업 창. 없으면 None.

    팝업은 제목 없는 창이고, 그림자용 창(SysShadow)이 몇 px 크게 하나 더
    뜬다. **작은 쪽**을 골라야 항목 위치가 밀리지 않는다 (connect.py 와 동일).
    """
    found = []
    for hwnd, title, r in winui.find_windows(min_w=1, min_h=1):
        if hwnd == main_hwnd or title or winui.class_of(hwnd) != "Window":
            continue
        if (r[2] - r[0]) < 400 and (r[3] - r[1]) > 100:
            found.append((hwnd, r))
    if not found:
        return None
    return min(found, key=lambda p: (p[1][2] - p[1][0]) * (p[1][3] - p[1][1]))


def _highlighted_band(popup_rect):
    """드롭다운에서 지금 반전된 항목의 세로 구간 (팝업 상대). 없으면 None."""
    path = str(TMP / "shopmine_tab_menu.bmp")
    winui.shot(popup_rect, path)
    w, h, px = winui.read_bmp32(path)
    ys = []
    for y in range(2, h - 2):
        hit = 0
        for x in range(20, w - 20):
            i = (y * w + x) * 4
            if winui.close_to((px[i + 2], px[i + 1], px[i]), MENU_HIGHLIGHT, 25):
                hit += 1
        if hit > 30:
            ys.append(y)
    return (min(ys), max(ys)) if ys else None


def _open_from_menu(main_hwnd, mdi_hwnd, name, timeout: float, log=print) -> bool:
    """[주문관리(O)] 메뉴에서 해당 탭을 다시 연다.

    항목은 방향키로 옮기되, **누르기 전에 지금 어느 항목이 반전돼 있는지
    픽셀로 확인**한다. 한 칸 어긋나도 다른 탭이 열릴 뿐이라 위험하진 않지만,
    그러면 확인 단계에서 걸려 중단되므로 애초에 맞춰서 누른다.
    """
    route = MENU_ROUTES.get(name)
    if route is None:
        raise TabError(f"'{name}' 탭이 닫혀 있고, 다시 여는 메뉴 경로를 모릅니다.")
    menu_key, item_y = route

    if not winui.bring_to_front(main_hwnd):
        raise TabError("샵마인 창을 앞으로 가져오지 못했습니다.")
    time.sleep(0.25)
    winui.key(menu_key, alt=True)

    popup = None
    for _ in range(20):
        time.sleep(0.2)
        popup = _menu_popup(main_hwnd)
        if popup:
            break
    if not popup:
        raise TabError("[주문관리(O)] 메뉴가 열리지 않았습니다.")

    for _ in range(14):
        band = _highlighted_band(popup[1])
        if band and band[0] <= item_y <= band[1]:
            winui.key(VK_RETURN)
            break
        winui.key(VK_DOWN)
        time.sleep(0.25)
    else:
        winui.key(VK_ESCAPE)
        time.sleep(0.2)
        winui.key(VK_ESCAPE)
        raise TabError(f"메뉴에서 [{name}] 항목을 찾지 못했습니다.")

    log(f"  메뉴 [주문관리] > [{name}] 로 탭을 다시 열었습니다")
    return _wait_for_tab(mdi_hwnd, name, timeout)


# --- 바깥에서 쓰는 함수 ---------------------------------------------

def ensure_tab(name: str = SHIPPING_TAB, timeout: float = 15.0, log=print) -> bool:
    """샵마인을 name 탭으로 맞춘다. 옮겼으면 True, 이미 그 탭이면 False.

    맞추지 못하면 `TabError` - 다른 탭에서 다음 단계를 진행하면 엉뚱한 화면을
    조작하게 된다.
    """
    main = _main_window()
    mdi = _mdi_client(main)

    now = current_tab(mdi)
    if now == name:
        log(f"  현재 탭: {name} - 그대로 진행")
        return False

    opened = open_tabs(mdi)
    if name in opened:
        log(f"  현재 탭이 '{now or '(읽을 수 없음)'}' 입니다 - [{name}] 탭으로 옮깁니다.")
        moved = _activate(mdi, opened[name], name, timeout)
    else:
        log(f"  [{name}] 탭이 닫혀 있습니다 - 메뉴에서 다시 엽니다.")
        moved = _open_from_menu(main, mdi, name, timeout, log=log)

    if not moved:
        raise TabError(
            f"[{name}] 탭으로 옮기지 못했습니다 (지금 '{current_tab(mdi) or '읽을 수 없음'}'). "
            "샵마인에서 직접 그 탭을 연 뒤 다시 실행해주세요.")
    log(f"  [{name}] 탭으로 이동 완료")
    return True
