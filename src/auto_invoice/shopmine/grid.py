"""배송중 탭 주문 목록(DataGridView)을 화면에서 읽고 행을 체크한다.

그리드 셀은 HWND를 갖지 않고, UI Automation으로 읽으면 샵마인이 크래시한다
(excel_io.py 상단 주석 참고). 그래서 유일하게 남은 방법이 '화면 픽셀을 보는
것'이다. 다행히 필요한 정보 두 가지는 픽셀로 아주 깨끗하게 갈린다.

  1. 행이 체크됐는지  - 체크박스가 파랑(0,95,184)이면 체크, 흰색(243,243,243)이면
     미체크, 무채색 회색(130,135,144)이면 그리드가 잠긴(로딩 중) 상태다.
  2. 송장번호(수정용)가 채워졌는지 - 그 셀 영역의 어두운 픽셀 수가
     비어 있으면 정확히 0, 값이 있으면 130개 이상이다.

두 번째가 이 모듈의 존재 이유다. 송장수정모드를 켜면 샵마인이
`송장번호(수정용)` 컬럼을 **현재 송장번호로 미리 채워두는데, 경동택배·직접전달
행만 비워둔다.** 즉 '수정용이 비어 있다' = '우리가 고쳐야 할 행'이고,
일괄등록 뒤에 '수정용이 채워졌다' = '업로드가 실제로 반영된 행'이다.
(공급사 조회에 실패한 건은 CSV에 없으므로 여전히 비어 있다.)

좌표는 2026-08-27 실측값이며, 창 상대좌표다. 행 높이·첫 행 위치는 매번
화면에서 다시 재기 때문에 상수는 '어디쯤을 볼지'의 힌트로만 쓰인다.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from . import winui

CHECKBOX_X = 44               # 체크박스 열의 가운데 x (창 상대)
CHECKBOX_BOX = 12             # 체크박스 사각형의 높이 (위 테두리 ~ 아래 테두리)
ROW_PITCH_HINT = 23           # 행 높이. 실제 값은 화면에서 다시 잰다
TRACK_EDIT_X = (180, 274)     # 송장번호(수정용) 셀의 x 범위
SELECT_ALL_HINT = (45, 391)   # 그리드 헤더의 [전체선택] 체크박스

BOX_BORDER = (98, 98, 98)     # 미체크 체크박스의 테두리
CHECK_BLUE_MARGIN = 40        # 파랑 판정: B - R 이 이보다 크면 체크됨
FILLED_DARK_MIN = 5           # 셀이 '채워짐'으로 볼 어두운 픽셀 수 (빈 셀은 0)

SCROLL_NOTCHES = 7            # 한 번에 굴릴 휠 칸수 (1칸 = 3행). 한 화면보다 작게


class GridError(RuntimeError):
    """그리드를 믿을 수 없는 상태여서 멈춰야 하는 상황."""


class Screen:
    """창을 한 번 캡처해 픽셀을 읽는 도우미.

    캡처는 '화면에 지금 보이는 것'을 뜬다. 그래서 찍기 전에 두 가지를 반드시
    정리한다. 안 하면 조용히 엉뚱한 픽셀을 읽는다.

      - 샵마인이 맨 앞인지: 다른 창이 위에 있으면 그 창을 찍는다. 실제로 이것
        때문에 110행짜리 목록이 2행으로 읽혀 60초 동안 회복하지 못했다.
      - 커서를 그리드 밖으로: 셀 위에 커서가 있으면 툴팁이 떠서 옆 행을 가린다.
    """

    def __init__(self, main_hwnd, park_cursor=True):
        r = winui.rect(main_hwnd)
        if park_cursor:
            winui.move_to(r.right - 60, r.bottom - 25)     # 상태표시줄 위 - 툴팁 없음
        if not _is_on_top(main_hwnd, r):
            winui.bring_to_front(main_hwnd)
            time.sleep(0.4)
            r = winui.rect(main_hwnd)
            if not _is_on_top(main_hwnd, r):
                raise GridError(
                    "샵마인 창이 다른 창에 가려져 있어 목록을 읽을 수 없습니다.")
        time.sleep(0.2)
        self.base = (r.left, r.top)
        path = str(Path(tempfile.gettempdir()) / "_shopmine_grid.bmp")
        winui.shot((r.left, r.top, r.right, r.bottom), path)
        self.w, self.h, self.px = winui.read_bmp32(path)

    def rgb(self, x, y):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return None
        i = (y * self.w + x) * 4
        return (self.px[i + 2], self.px[i + 1], self.px[i])

    def to_screen(self, x, y):
        return self.base[0] + x, self.base[1] + y


def _is_on_top(main_hwnd, r) -> bool:
    """그리드 한복판이 정말 샵마인 창인지 (다른 창이 덮고 있지 않은지)."""
    cx = (r.left + r.right) // 2
    cy = r.top + (r.bottom - r.top) * 3 // 4
    return winui.is_descendant(winui.window_at(cx, cy), main_hwnd)


def _is_blue(c) -> bool:
    return c is not None and c[2] - c[0] > CHECK_BLUE_MARGIN


def _is_check_blue(c) -> bool:
    """체크된 체크박스의 진한 파랑인가.

    '선택된 행'의 연한 하늘색 배경(145,202,247)도 파란 계열이라 _is_blue 만으로는
    갈리지 않는다. 실제로 그 배경을 체크박스 윗변으로 오인해 행 위치가 4px씩
    밀렸고, 110행짜리 목록이 2행으로 읽혔다. 진한 쪽(빨강 성분이 낮은 쪽)만
    체크박스로 본다.
    """
    return c is not None and c[2] - c[0] > 60 and c[0] < 140


def _box_top(scr: Screen, y: int) -> str | None:
    """y가 체크박스 사각형의 윗변이면 'checked'/'unchecked', 아니면 None."""
    top = scr.rgb(CHECKBOX_X, y)
    bottom = scr.rgb(CHECKBOX_X, y + CHECKBOX_BOX)
    middle = scr.rgb(CHECKBOX_X, y + CHECKBOX_BOX // 2)
    if _is_check_blue(top) and _is_check_blue(bottom) and _is_blue(middle):
        return "checked"
    if top == BOX_BORDER and bottom == BOX_BORDER and middle is not None \
            and min(middle) >= 200:
        return "unchecked"
    return None


def _filled(scr: Screen, cy: int) -> int:
    """송장번호(수정용) 셀의 어두운 픽셀 수."""
    n = 0
    x0, x1 = TRACK_EDIT_X
    for y in range(cy - 6, cy + 7):
        for x in range(x0, x1):
            c = scr.rgb(x, y)
            if c is None:
                continue
            if (c[0] * 299 + c[1] * 587 + c[2] * 114) // 1000 < 128:
                n += 1
    return n


class Row:
    __slots__ = ("cy", "checked", "filled")

    def __init__(self, cy, checked, filled):
        self.cy = cy                # 창 상대 y (체크박스 중심)
        self.checked = checked
        self.filled = filled        # 송장번호(수정용)가 채워져 있는가


def _scrollbars(main_hwnd):
    """그리드 영역의 (세로, 가로) 스크롤바. 필요 없으면 샵마인이 숨기므로 None일 수 있다."""
    grid_area = winui.u.GetParent(select_all_checkbox(main_hwnd))
    vertical = horizontal = None
    for k in winui.children(grid_area) + winui.children(winui.u.GetParent(grid_area)):
        if not winui.u.IsWindowVisible(k) or "SCROLLBAR" not in winui.class_of(k).upper():
            continue
        r = winui.rect(k)
        if r.bottom - r.top > r.right - r.left:
            vertical = k
        else:
            horizontal = k
    return vertical, horizontal


def bounds(main_hwnd):
    """데이터 행이 그려지는 영역의 (위 y, 아래 y) - 창 상대.

    헤더 [전체선택] 체크박스와 그리드 영역 창에서 역산한다. 스크롤바 위치를
    기준으로 삼으면 행이 몇 개 없어 스크롤바가 숨겨졌을 때 실패한다.
    """
    base = winui.rect(main_hwnd)
    btn = select_all_checkbox(main_hwnd)
    header = winui.rect(btn)
    area = winui.rect(winui.u.GetParent(btn))
    top = header.bottom - base.top + 4
    bottom = area.bottom - base.top - 2
    _v, horizontal = _scrollbars(main_hwnd)
    if horizontal is not None:
        bottom = winui.rect(horizontal).top - base.top - 2
    return top, bottom


def select_all_checkbox(main_hwnd):
    """헤더의 [전체선택] 체크박스. WinForms CheckBox 라 HWND가 있다."""
    base = winui.rect(main_hwnd)

    def hit(k):
        if not winui.u.IsWindowVisible(k) or winui.ctrl_text(k) != "":
            return False
        r = winui.rect(k)
        if r.right - r.left > 30 or r.bottom - r.top > 30:
            return False
        rel = (r.left - base.left, r.top - base.top)
        return (abs(rel[0] - SELECT_ALL_HINT[0]) < 30
                and abs(rel[1] - SELECT_ALL_HINT[1]) < 40)

    btn = winui.find_child(main_hwnd, "BUTTON", hit)
    if btn is None:
        raise GridError("그리드 헤더의 [전체선택] 체크박스를 찾지 못했습니다.")
    return btn


def read_rows(main_hwnd, top=None, bottom=None) -> list[Row]:
    """지금 화면에 보이는 데이터 행들을 읽는다.

    첫 행 위치와 행 높이를 화면에서 직접 재고, 그 간격으로 아래로 훑는다.
    체크박스가 더 안 나오면 거기가 목록 끝이다.
    """
    if top is None:
        top, bottom = bounds(main_hwnd)
    scr = Screen(main_hwnd)

    first = None
    for y in range(top, bottom - CHECKBOX_BOX):
        if _box_top(scr, y):
            first = y
            break
    if first is None:
        return []

    second = None
    for y in range(first + CHECKBOX_BOX + 1, min(first + 60, bottom - CHECKBOX_BOX)):
        if _box_top(scr, y):
            second = y
            break
    pitch = (second - first) if second else ROW_PITCH_HINT

    rows = []
    y = first
    while y + CHECKBOX_BOX <= bottom:
        state = _box_top(scr, y)
        if state is None:
            break
        cy = y + CHECKBOX_BOX // 2
        rows.append(Row(cy, state == "checked", _filled(scr, cy) >= FILLED_DARK_MIN))
        y += pitch
    return rows


def _signature(rows) -> tuple:
    return tuple((r.cy, r.checked, r.filled) for r in rows)


def wait_ready(main_hwnd, timeout: float = 60.0, log=print) -> list[Row]:
    """그리드가 안정될 때까지 기다렸다가 행을 읽는다.

    세 가지 과도 상태를 다 피해야 한다.

      - 로딩 중에는 체크박스가 무채색 회색이라 행이 하나도 안 읽힌다. 그 상태로
        다음 단계에 가면 클릭이 조용히 무시된다.
      - 필터를 적용/해제한 직후에는 목록이 다시 그려지는 동안 일부만 보인다.
        실제로 필터초기화 직후에 2행만 읽혔는데 실제로는 110행이었다.
      - 반대로 이전 화면이 잠깐 남아 있기도 한다.

    그래서 두 가지를 함께 본다. (1) 두 번 연속 같게 읽힐 것, (2) 세로
    스크롤바가 있다면 - 즉 한 화면에 안 들어가는 목록이라면 - 화면이 행으로
    가득 차 있을 것. 스크롤이 되는 목록인데 몇 줄만 읽혔다면 그건 아직
    그리는 중이라는 뜻이다.
    """
    top, bottom = bounds(main_hwnd)
    capacity = (bottom - top) // ROW_PITCH_HINT
    end = time.time() + timeout
    previous = None
    while True:
        rows = read_rows(main_hwnd, top, bottom)
        scrollable = _scrollbars(main_hwnd)[0] is not None
        full_enough = (not scrollable) or len(rows) >= capacity - 1
        if rows and full_enough and _signature(rows) == previous:
            return rows
        if time.time() >= end:
            if not full_enough:
                raise GridError(
                    f"목록이 계속 불안정합니다 ({len(rows)}행만 읽힘, 화면은 "
                    f"{capacity}행이 들어갈 크기). 샵마인이 아직 화면을 그리는 중일 수 있습니다.")
            return rows
        previous = _signature(rows)
        time.sleep(0.7)


def _click_checkbox(main_hwnd, cy, label=""):
    base = winui.rect(main_hwnd)
    ax, ay = base.left + CHECKBOX_X, base.top + cy
    if not winui.is_descendant(winui.window_at(ax, ay), main_hwnd):
        raise GridError(f"[{label}] 체크박스가 다른 창에 가려져 있습니다.")
    try:
        winui.move_click(ax, ay)
    except RuntimeError as e:
        raise GridError(f"[{label}] {e} (실행 중 마우스를 움직이면 이렇게 됩니다)") from e


def scroll_to_top(main_hwnd, log=print) -> None:
    base = winui.rect(main_hwnd)
    sb, _h = _scrollbars(main_hwnd)
    if sb is None:
        return
    for _ in range(80):
        info = winui.scroll_info(sb)
        if info is None or info[0] == 0:
            return
        winui.wheel(base.left + 700, base.top + 700, -SCROLL_NOTCHES)
        time.sleep(0.4)
    log("  경고: 목록을 맨 위로 올리지 못했습니다.")


def _pages(main_hwnd, log=print):
    """맨 위부터 한 화면씩 내려가며 (행 목록)을 내준다."""
    base = winui.rect(main_hwnd)
    sb, _h = _scrollbars(main_hwnd)
    scroll_to_top(main_hwnd, log=log)
    while True:
        yield wait_ready(main_hwnd, log=log)
        info = winui.scroll_info(sb) if sb is not None else None
        if info is None:            # 스크롤바가 없다 = 한 화면에 다 보인다
            return
        pos, max_pos, _page = info
        if pos >= max_pos:
            return
        winui.wheel(base.left + 700, base.top + 700, SCROLL_NOTCHES)
        time.sleep(0.8)
        if winui.scroll_info(sb)[0] == pos:      # 더 안 내려가면 끝
            return


def _click_select_all(main_hwnd, btn) -> None:
    """헤더 [전체선택] 체크박스를 한 번 누른다 (누를 때마다 전체 켜기/끄기 토글).

    이 체크박스는 HWND가 있으므로 커서를 쓰지 않는다. 부른 쪽에서 첫 화면이
    실제로 바뀌었는지 확인하므로, 눌렸는지 여부는 그쪽에서 판정된다.
    """
    winui.bring_to_front(main_hwnd)
    time.sleep(0.3)
    winui.press_button(btn)
    time.sleep(1.2)


def check_all_filtered(main_hwnd, log=print) -> int:
    """지금 필터에 걸린 행 전부를 헤더 [전체선택]으로 체크한다.

    전체선택은 '보이는 화면'이 아니라 '필터에 걸린 전체'에 적용되므로 스크롤할
    필요가 없다. 다만 누를 때마다 켜기/끄기가 뒤집히므로, 눌러본 뒤 첫 화면이
    실제로 체크됐는지 확인하고 아니면 한 번 더 누른다.
    """
    btn = select_all_checkbox(main_hwnd)
    rows = wait_ready(main_hwnd, log=log)
    if not rows:
        log("  이 필터에 해당하는 주문이 없습니다 - 건너뜁니다.")
        return 0

    for attempt in (1, 2, 3):
        if all(row.checked for row in rows):
            log(f"  전체 체크 확인 (첫 화면 {len(rows)}행)")
            return len(rows)
        _click_select_all(main_hwnd, btn)
        rows = wait_ready(main_hwnd, log=log)
    raise GridError("[전체선택]을 눌러도 행이 체크되지 않았습니다.")


def header_center_y(main_hwnd) -> int:
    """그리드 헤더 줄의 세로 가운데 (창 상대 y).

    헤더 [전체선택] 체크박스가 헤더 줄 안에 있으므로 그 위치로 역산한다
    (bounds()가 위쪽 경계를 잡는 방식과 같다).
    """
    base = winui.rect(main_hwnd)
    header = winui.rect(select_all_checkbox(main_hwnd))
    return (header.top + header.bottom) // 2 - base.top


def filled_first(rows) -> bool:
    """채워진 행이 전부 빈 행보다 위에 있는가 (정렬이 먹었는지 판정)."""
    seen_empty = False
    for row in rows:
        if row.filled and seen_empty:
            return False
        seen_empty = seen_empty or not row.filled
    return True


def sort_filled_first(main_hwnd, log=print) -> bool:
    """'송장번호(수정용)' 컬럼 헤더를 눌러 채워진 행을 위로 올린다.

    왜 정렬부터 하나: 일괄등록으로 채워진 행은 목록 여기저기에 흩어져 있어서,
    그냥 체크하려면 수십 행을 스크롤하며 전부 훑어야 한다. 헤더를 두 번 눌러
    (한 번은 오름차순, 한 번은 내림차순) 채워진 행을 위로 모아두면 위에서부터
    연속으로 체크하면 되고, 빈 행이 처음 나오는 순간 아래는 볼 필요가 없다.

    누른 뒤 반드시 화면으로 확인한다 - 정렬이 안 먹었는데 '위쪽만 보면 된다'고
    믿으면 채워진 행을 통째로 빠뜨린다. 확인되지 않으면 False를 돌려주고,
    부른 쪽은 예전처럼 전체를 훑는다 (느릴 뿐 결과는 같다).
    """
    # 한 화면에 다 보이는데 채워진 행이 하나도 없으면 정렬할 것이 없다.
    # (경동택배/직접 필터에 걸린 행이 몇 개뿐이고 이번 업로드에 해당 건이
    # 없을 때가 그렇다 - 이때 헤더를 눌러봐야 소용없고 경고만 남는다.)
    if _scrollbars(main_hwnd)[0] is None:
        rows = wait_ready(main_hwnd, log=log)
        if not any(r.filled for r in rows):
            log(f"  이 필터에 송장이 채워진 행이 없습니다 ({len(rows)}행) - 정렬 건너뜀")
            return False

    cx, cy = sum(TRACK_EDIT_X) // 2, header_center_y(main_hwnd)
    # 먼저 두 번(오름차순 -> 내림차순) 누른다. 빈 값이 오름차순에서 위로 가는
    # 그리드라면 이걸로 채워진 행이 위에 온다. 아니면 한 번씩 더 눌러 반대
    # 방향도 본다 - 클릭이 조용히 무시된 경우에도 이 재시도가 살려준다.
    for clicks in (2, 1, 1):
        for _ in range(clicks):
            if not winui.bring_to_front(main_hwnd):
                log("  경고: 샵마인 창을 앞으로 가져오지 못해 정렬을 건너뜁니다.")
                return False
            ok, msg = winui.safe_click(main_hwnd, (cx, cy), label="송장번호(수정용) 헤더")
            if not ok:
                log(f"  경고: {msg} - 정렬 없이 진행합니다.")
                return False
            time.sleep(1.2)
        rows = wait_ready(main_hwnd, log=log)
        if rows and rows[0].filled and filled_first(rows):
            log(f"  송장번호(수정용) 정렬 확인 - 채워진 행이 위로 올라왔습니다 "
                f"(첫 화면 {sum(1 for r in rows if r.filled)}/{len(rows)}행)")
            return True
    log("  경고: 헤더를 눌러도 채워진 행이 위로 오지 않았습니다 - 목록 전체를 훑습니다.")
    return False


def check_filled_rows(main_hwnd, log=print, stop_at_empty=False) -> int:
    """송장번호(수정용)가 채워진 행만 체크한다 (화면을 내려가며 전부).

    이미 체크된 행은 건드리지 않으므로 몇 번을 돌려도 결과가 같다. 그래서
    스크롤이 겹쳐도 안전하다.

    stop_at_empty: sort_filled_first로 '채워진 행이 위'라는 걸 확인했을 때만
    참으로 준다. 빈 행이 나오는 화면까지만 보고 멈춘다 - 그 아래는 전부 빈
    행이다. 정렬을 확인하지 못했으면 거짓으로 두고 끝까지 훑는다.
    """
    total = 0
    for page in _pages(main_hwnd, log=log):
        targets = [row.cy for row in page if row.filled and not row.checked]
        done_here = stop_at_empty and any(not row.filled for row in page)
        if not targets:
            if done_here:
                break
            continue
        for cy in targets:
            _click_checkbox(main_hwnd, cy, label="송장 채워진 행")
            time.sleep(0.3)
        after = {row.cy: row for row in wait_ready(main_hwnd, log=log)}
        missed = [cy for cy in targets if not (cy in after and after[cy].checked)]
        for cy in missed:
            _click_checkbox(main_hwnd, cy, label="송장 채워진 행(재시도)")
            time.sleep(0.5)
        if missed:
            after = {row.cy: row for row in wait_ready(main_hwnd, log=log)}
            still = [cy for cy in missed if not (cy in after and after[cy].checked)]
            if still:
                raise GridError(
                    f"체크되지 않은 행이 남았습니다 (창 상대 y={still}). "
                    "화면이 갱신 중이거나 목록이 움직였을 수 있습니다.")
        total += len(targets)
        log(f"  이 화면에서 {len(targets)}건 체크 (누적 {total}건)")
        if done_here:
            log("  빈 행이 나와 여기까지만 봅니다 (정렬돼 있어 아래는 전부 빈 행)")
            break
    return total


def clear_all_checks(main_hwnd, log=print) -> None:
    """체크를 전부 해제해 '아무것도 선택되지 않은' 상태에서 시작한다.

    지난 실행이나 사람이 남겨둔 체크가 그대로 있으면 [송장번호수정] 확인창의
    건수가 어긋나 전부 멈춘다. 시작할 때 한 번 정리하고 들어간다.

    '전부 켰다가 한 번 더 눌러 전부 끈다'는 순서를 쓴다. 화면 밖에 남아 있는
    체크는 눈으로 확인할 수 없는데, 전체선택은 필터 전체에 적용되므로 이렇게
    하면 보이지 않는 행까지 확실히 정리된다.
    """
    btn = select_all_checkbox(main_hwnd)
    rows = wait_ready(main_hwnd, log=log)
    if not rows:
        return
    for _ in range(3):
        if all(row.checked for row in rows):
            break
        _click_select_all(main_hwnd, btn)
        rows = wait_ready(main_hwnd, log=log)
    else:
        raise GridError("[전체선택]이 동작하지 않아 선택 상태를 초기화하지 못했습니다.")

    _click_select_all(main_hwnd, btn)
    rows = wait_ready(main_hwnd, log=log)
    if any(row.checked for row in rows):
        raise GridError("선택 상태를 해제하지 못했습니다.")
    log("  선택 상태 초기화 완료")
