"""작업을 시작하기 전에 샵마인의 쇼핑몰 연결 상태를 확인하고, 끊긴 곳을 다시 연결한다.

왜 필요한가: 5단계 [송장번호수정]은 샵마인이 **실제로 쇼핑몰에 접속해서**
송장을 등록한다. 연결이 끊긴 쇼핑몰의 주문은 이때 조용히 실패하거나 결과 창에
오류로 남는다. 그래서 화면을 건드리기 전에 먼저 전부 연결돼 있는지 본다.

화면 흐름 (2026-08-28 실측):

    메뉴바 오른쪽 [쇼핑몰연결(Y)]  --Alt+Y--> 드롭다운
      전부새로연결(A)...
      연결안된 쇼핑몰 연결재시도(C)...
      쇼핑몰연결창(W)...            <- 이걸 연다
      쇼핑몰연결창(간소화)...
      ------- 이하 쇼핑몰별 '01.지마켓(howk9318) - 연결됨' 목록 -------

    [쇼핑몰 연결] 창
      연결상태 콤보(전체/연결안됨/연결됨) + 쇼핑몰 콤보 + 그리드
      [체크한 쇼핑몰 연결재시도(R)] [연결안된 쇼핑몰 연결재시도(C)]
      '연결 재시도 결과' EDIT + [닫기(ESC)]

드롭다운 목록으로도 상태를 알 수 있지만(연결됨이면 초록 아이콘) 목록이 화면
높이를 넘어가 스크롤해야 한다. [쇼핑몰 연결] 창은 **연결상태 필터**가 있어
'연결안됨'만 걸면 스크롤 없이 한눈에 확인되고, 버튼·콤보·결과칸이 전부 진짜
HWND라 좌표 클릭도 거의 필요 없다. 그래서 이쪽을 쓴다.

주의할 점 두 가지:

1. 이 창의 제목이 하필 `upload.BUSY_WINDOW`("쇼핑몰 연결")와 같다. 열어둔 채로
   두면 `upload.wait_until_idle()`이 "아직 쇼핑몰 연결 중"으로 오해해서 5분을
   기다린다. **끝나면 반드시 닫는다** (`close_window`).
2. 이 창은 잠시 두면 저절로 닫힌다(실측). 그래서 열고-확인하고-닫는 것을 한
   호출 안에서 끝낸다.

그리드에 행이 있는지는 픽셀로 본다. 샵마인 본체(.NET)는 접근성 API로 읽지
않는다는 이 프로젝트의 규칙 그대로다(excel_io.py 상단 주석 참고). 행이 하나도
없으면 그리드 오른쪽 절반이 완전히 흰색이고, 행이 있으면 연결상태 아이콘 /
연결상태메세지 / [상세보기] / [연결재시도] 글자가 반드시 거기에 그려진다.
"""

from __future__ import annotations

import ctypes
import tempfile
import time
from pathlib import Path

from . import winui

CONN_WINDOW = "쇼핑몰 연결"
STATE_FILTER_TARGET = "연결안됨"
RESULT_LABEL = "연결 재시도 결과"
RETRY_BUTTON = "연결안된"          # '연결안된 쇼핑몰 연결재시도(&C)'
CLOSE_BUTTON = "닫기"

VK_Y = 0x59
VK_DOWN = 0x28
VK_HOME = 0x24
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

# 드롭다운에서 선택된 항목의 배경색 (Windows 11 기본 테마)
MENU_HIGHLIGHT = (181, 215, 242)

TMP = Path(tempfile.gettempdir())

# 드롭다운 안에서 '쇼핑몰연결창(W)...' 항목이 차지하는 세로 위치(팝업 상대).
# 항목 높이는 22px이고 첫 항목이 y=10 부터라 세 번째 항목의 한가운데다.
MENU_ITEM_WINDOW_Y = 63

# 그리드에서 '행이 있는지'를 볼 가로 구간 (그리드 폭에 대한 비율).
# 왼쪽 절반에는 행이 없을 때 '조회된 쇼핑몰 계정이 없습니다.' 안내문이 걸쳐
# 있어서 쓸 수 없다. 오른쪽 절반은 행이 없으면 완전히 비어 있다.
GRID_SCAN_X = (0.40, 0.98)
# 헤더 아래 (160,160,160) 구분선까지 건너뛴다. 이 선을 세면 목록이 비어 있어도
# '행이 있다'가 되어버린다 (실제로 그랬다).
GRID_HEADER_H = 30
GRID_INK = 200               # 이보다 어두운 픽셀만 '글자'로 센다
                             # (행 구분선 같은 연한 회색 229는 제외)
GRID_INK_MIN = 40            # 이만큼 넘게 어두운 픽셀이 있으면 '행이 있다'

# 재연결이 끝났다고 볼 조건: 결과 칸의 글자가 이만큼 그대로일 것
STABLE_FOR = 10.0


WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E
SMTO_ABORTIFHUNG = 0x0002


class ConnectError(RuntimeError):
    """쇼핑몰 연결 확인을 안전하게 끝내지 못한 상황."""


def _text_or_none(hwnd, timeout_ms: int = 1500):
    """컨트롤 글자를 **타임아웃을 두고** 읽는다. 못 읽으면 None.

    재연결 중에는 샵마인이 쇼핑몰에 로그인하느라 UI 스레드가 통째로 멈춘다.
    그때 `winui.ctrl_text`(SendMessage)로 읽으면 같이 멈춰버리므로, 여기서는
    SendMessageTimeout 으로 읽고 '못 읽음'을 그대로 돌려준다 - 못 읽는 것 자체가
    '아직 진행 중'이라는 신호다.
    """
    res = ctypes.c_size_t(0)
    ok = winui.u.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0,
                                     SMTO_ABORTIFHUNG, timeout_ms,
                                     ctypes.byref(res))
    if not ok:
        return None
    n = int(res.value)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ok = winui.u.SendMessageTimeoutW(hwnd, WM_GETTEXT, n + 1,
                                     ctypes.cast(buf, ctypes.c_void_p),
                                     SMTO_ABORTIFHUNG, timeout_ms,
                                     ctypes.byref(res))
    return buf.value if ok else None


def _main_window():
    wins = winui.find_windows(title_startswith="ShopMine::")
    if not wins:
        raise ConnectError("샵마인이 실행 중이지 않습니다.")
    return wins[0][0]


def _conn_window():
    wins = winui.find_windows(title_equals=CONN_WINDOW)
    return wins[0][0] if wins else None


def _menu_popup(main_hwnd):
    """[쇼핑몰연결(Y)] 드롭다운 팝업 창. 없으면 None.

    팝업은 샵마인 프로세스가 소유한 제목 없는 창이고, 세로로 길고 좁다.
    그림자용 창이 5px 더 크게 하나 더 뜨므로 **작은 쪽**을 고른다 - 그림자 창
    기준으로 좌표를 재면 항목 위치가 그만큼 밀린다.
    """
    found = []
    for hwnd, _title, r in winui.find_windows(min_w=1, min_h=1):
        if hwnd != main_hwnd and (r[2] - r[0]) < 400 and (r[3] - r[1]) > 400:
            found.append((hwnd, r))
    if not found:
        return None
    return min(found, key=lambda p: (p[1][2] - p[1][0]) * (p[1][3] - p[1][1]))


# --- 창 열고 닫기 ---------------------------------------------------
def open_window(log=print, timeout: float = 15.0):
    """[쇼핑몰연결(Y)] > [쇼핑몰연결창(W)...] 으로 연결 상태 창을 연다.

    메뉴는 Alt+Y 로 연다(좌표 불필요). 항목은 방향키로 옮기되, **누르기 전에
    지금 어느 항목이 반전돼 있는지 픽셀로 확인**한다 - 한 칸만 어긋나도
    [전부새로연결]이나 [연결안된 쇼핑몰 연결재시도]가 실행되기 때문이다.
    """
    already = _conn_window()
    if already:
        return already

    main = _main_window()
    if not winui.bring_to_front(main):
        raise ConnectError("샵마인 창을 앞으로 가져오지 못했습니다.")
    time.sleep(0.5)

    winui.key(VK_Y, alt=True)
    popup = None
    for _ in range(10):
        time.sleep(0.4)
        popup = _menu_popup(main)
        if popup:
            break
    if not popup:
        raise ConnectError("[쇼핑몰연결(Y)] 메뉴가 열리지 않았습니다.")

    for _ in range(8):
        band = _highlighted_band(popup[1])
        if band and band[0] <= MENU_ITEM_WINDOW_Y <= band[1]:
            winui.key(VK_RETURN)
            break
        winui.key(VK_DOWN)
        time.sleep(0.35)
    else:
        winui.key(VK_ESCAPE)
        raise ConnectError("메뉴에서 [쇼핑몰연결창(W)...] 항목을 찾지 못했습니다.")

    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        hwnd = _conn_window()
        if hwnd:
            log("  [쇼핑몰 연결] 창을 열었습니다")
            return hwnd
    raise ConnectError("[쇼핑몰 연결] 창이 열리지 않았습니다.")


def _highlighted_band(popup_rect):
    """드롭다운에서 지금 반전된 항목의 세로 구간 (팝업 상대). 없으면 None."""
    path = str(TMP / "shopmine_menu.bmp")
    winui.shot(popup_rect, path)
    w, h, px = winui.read_bmp32(path)
    ys = []
    for y in range(6, min(h, 120)):
        hit = 0
        for x in range(30, min(w, 300)):
            i = (y * w + x) * 4
            if winui.close_to((px[i + 2], px[i + 1], px[i]), MENU_HIGHLIGHT, 25):
                hit += 1
        if hit > 30:
            ys.append(y)
    return (min(ys), max(ys)) if ys else None


def close_window(log=print) -> None:
    """[쇼핑몰 연결] 창을 닫는다.

    닫지 않으면 `upload.wait_until_idle()`이 이 창을 '쇼핑몰 연결 진행 중'으로
    오해한다 (제목이 같다).
    """
    hwnd = _conn_window()
    if not hwnd:
        return
    btn = winui.find_child(hwnd, "BUTTON",
                           lambda k: winui.ctrl_text(k).startswith(CLOSE_BUTTON))
    if btn is None:
        raise ConnectError("[쇼핑몰 연결] 창의 [닫기] 버튼을 찾지 못했습니다.")
    winui.bring_to_front(hwnd)
    time.sleep(0.3)
    winui.press_button(btn)
    if winui.wait_for_window_gone(title_equals=CONN_WINDOW, timeout=10.0):
        log("  [쇼핑몰 연결] 창을 닫았습니다")
    else:
        raise ConnectError(
            "[쇼핑몰 연결] 창이 닫히지 않았습니다. 열어두면 다음 단계에서 "
            "'쇼핑몰 연결 중'으로 오해하므로 직접 닫아주세요.")


# --- 창 안의 컨트롤 -------------------------------------------------
def _state_combo(hwnd):
    """'연결상태 :' 콤보박스. 두 콤보 중 왼쪽 것이다."""
    combos = [k for k in winui.children(hwnd) if winui.class_of(k) == "COMBOBOX"]
    if not combos:
        raise ConnectError("[쇼핑몰 연결] 창에서 연결상태 필터를 찾지 못했습니다.")
    return min(combos, key=lambda k: winui.rect(k).left)


def _result_label(hwnd):
    return winui.find_child(hwnd, "STATIC",
                            lambda k: winui.ctrl_text(k).strip() == RESULT_LABEL)


def _grid_rect(hwnd):
    """그리드의 화면 좌표 (left, top, right, bottom).

    연결상태 콤보 아래 ~ '연결 재시도 결과' 라벨 위 사이에서 가장 큰 자식 창을
    고른다. 창을 키우거나 줄여도 따라간다.
    """
    top_limit = winui.rect(_state_combo(hwnd)).bottom
    label = _result_label(hwnd)
    if label is None:
        raise ConnectError("[쇼핑몰 연결] 창에서 '연결 재시도 결과' 칸을 찾지 못했습니다.")
    bottom_limit = winui.rect(label).top

    best = None
    for k in winui.children(hwnd):
        if winui.class_of(k) != "Window":
            continue
        r = winui.rect(k)
        if r.top <= top_limit or r.bottom > bottom_limit:
            continue
        area = (r.right - r.left) * (r.bottom - r.top)
        if best is None or area > best[0]:
            best = (area, (r.left, r.top, r.right, r.bottom))
    if best is None:
        raise ConnectError("[쇼핑몰 연결] 창에서 목록(그리드)을 찾지 못했습니다.")
    return best[1]


# --- 연결상태 필터 --------------------------------------------------
def set_state_filter(hwnd, value: str = STATE_FILTER_TARGET, log=print) -> None:
    """연결상태 콤보를 원하는 값으로 맞춘다.

    항목 순서를 외워두지 않는다. 목록을 펼친 뒤 Home 으로 맨 위로 갔다가 한 칸씩
    내려가면서 **콤보에 표시되는 글자**를 매번 읽어 원하는 값일 때 Enter 를
    누른다. 목록이 바뀌어도 따라간다.
    """
    combo = _state_combo(hwnd)
    if winui.ctrl_text(combo).strip() == value:
        return

    winui.bring_to_front(hwnd)
    time.sleep(0.3)
    r = winui.rect(combo)
    winui.move_click((r.left + r.right) // 2, (r.top + r.bottom) // 2, dwell=0.4)
    time.sleep(0.6)
    winui.key(VK_HOME)
    time.sleep(0.3)

    for _ in range(12):
        if winui.ctrl_text(combo).strip() == value:
            winui.key(VK_RETURN)
            time.sleep(1.0)
            break
        winui.key(VK_DOWN)
        time.sleep(0.25)
    else:
        winui.key(VK_ESCAPE)
        raise ConnectError(f"연결상태 필터에 '{value}' 항목이 없습니다.")

    got = winui.ctrl_text(combo).strip()
    if got != value:
        raise ConnectError(f"연결상태 필터를 '{value}'로 바꾸지 못했습니다 (지금 '{got}').")
    log(f"  연결상태 필터: {value}")


# --- 목록에 행이 있는지 ---------------------------------------------
def _grid_row_bands(hwnd) -> list[tuple[int, int]]:
    """그리드 데이터 영역에서 '내용이 있는' 가로줄들의 (y, 어두운 픽셀 수).

    글자가 있는 줄만 담기므로, 행이 하나도 없으면 빈 리스트가 된다.
    """
    path = str(TMP / "shopmine_conn_grid.bmp")
    gr = _grid_rect(hwnd)
    winui.shot(gr, path)
    w, h, px = winui.read_bmp32(path)
    x1 = int(w * GRID_SCAN_X[0])
    x2 = int(w * GRID_SCAN_X[1])
    out = []
    for y in range(GRID_HEADER_H, h - 2):
        ink = 0
        for x in range(x1, x2):
            i = (y * w + x) * 4
            if px[i] < GRID_INK or px[i + 1] < GRID_INK or px[i + 2] < GRID_INK:
                ink += 1
        if ink:
            out.append((y, ink))
    return out


def count_disconnected(hwnd, log=print) -> int:
    """'연결안됨'으로 필터된 목록에 몇 행이 보이는지.

    정확한 건수가 아니라 **0인지 아닌지**가 중요하다. 0이 아니면 화면에 보이는
    행 수를 세어 알려준다 (스크롤 아래로 더 있을 수 있다).
    """
    bands = _grid_row_bands(hwnd)
    total_ink = sum(ink for _y, ink in bands)
    if total_ink < GRID_INK_MIN:
        return 0
    # 행 높이는 22px 남짓이다. 글자가 있는 줄들을 끊어 세면 행 수가 나온다.
    rows, prev = 0, None
    for y, _ink in bands:
        if prev is None or y - prev > 4:
            rows += 1
        prev = y
    log(f"  연결되지 않은 쇼핑몰이 화면에 {rows}개 보입니다")
    return rows


# --- 재연결 --------------------------------------------------------
def retry_disconnected(hwnd, log=print, timeout: float = 300.0) -> str:
    """[연결안된 쇼핑몰 연결재시도(C)]를 누르고 끝날 때까지 기다린다.

    끝났는지는 '연결 재시도 결과' 칸의 글자가 더 이상 변하지 않는 것으로 본다.
    쇼핑몰마다 로그인을 다시 하므로 몇 분이 걸릴 수 있다.
    """
    btn = winui.find_child(hwnd, "BUTTON",
                           lambda k: winui.ctrl_text(k).startswith(RETRY_BUTTON))
    if btn is None:
        raise ConnectError("[연결안된 쇼핑몰 연결재시도] 버튼을 찾지 못했습니다.")
    result = winui.find_child(hwnd, "EDIT", lambda k: True)

    winui.bring_to_front(hwnd)
    time.sleep(0.3)
    log("  [연결안된 쇼핑몰 연결재시도] 실행")
    winui.press_button(btn)

    last, stable_since = None, time.time()
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2.0)
        now = _text_or_none(result) if result else ""
        busy = now is None or not winui.u.IsWindowEnabled(btn)
        if busy or now != last:
            # 아직 쇼핑몰에 붙는 중이다. 로그인하느라 UI 스레드가 멈춰 있으면
            # 글자를 아예 못 읽는데(None), 그것도 '진행 중'으로 본다.
            if not busy:
                last = now
            stable_since = time.time()
            continue
        if time.time() - stable_since >= STABLE_FOR:
            break
    else:
        log(f"  경고: {timeout:.0f}초 안에 재연결이 끝나지 않았습니다.")

    text = (last or "").strip()
    if text:
        for line in text.splitlines():
            if line.strip():
                log(f"    {line.strip()}")
    return text


# --- 바깥에서 쓰는 함수 ---------------------------------------------
def ensure_connected(log=print, attempts: int = 2) -> None:
    """쇼핑몰이 전부 연결돼 있는지 확인하고, 아니면 재연결한 뒤 다시 확인한다.

    전부 연결된 것을 확인하지 못하면 `ConnectError`를 던진다 - 연결이 끊긴 채로
    송장을 반영하면 그 쇼핑몰 주문이 조용히 실패하기 때문이다.
    """
    hwnd = open_window(log=log)
    try:
        for attempt in range(1, attempts + 1):
            set_state_filter(hwnd, log=log)
            if count_disconnected(hwnd, log=log) == 0:
                log("  쇼핑몰이 전부 연결돼 있습니다")
                return
            if attempt == attempts:
                break
            retry_disconnected(hwnd, log=log)
            # 재시도 뒤에는 목록을 다시 조회해야 한다. 필터를 한 번 풀었다
            # 다시 걸면 확실하게 다시 읽는다.
            set_state_filter(hwnd, value="(전체)", log=log)
        raise ConnectError(
            "재연결을 시도했는데도 연결되지 않은 쇼핑몰이 남아 있습니다. "
            "샵마인 [쇼핑몰연결(Y)] > [쇼핑몰연결창]에서 직접 확인해주세요.")
    finally:
        try:
            close_window(log=log)
        except ConnectError as e:
            log(f"  경고: {e}")
