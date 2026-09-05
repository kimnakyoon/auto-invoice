"""표준 열기/저장 대화상자(#32770)의 '파일 이름' 칸을 채우고 확정한다.

엑셀 내보내기(다른 이름으로 저장, export.py)와 발송정보일괄등록(열기,
upload.py)이 같은 공용 대화상자를 쓴다 - 파일 이름 칸은 ComboBoxEx32 안의
Edit(FILENAME_EDIT_IDS), 확정 버튼은 IDOK(1). 두 곳이 따로 구현하면 한쪽에서
겪은 사고가 다른 쪽에 반영되지 않는다.

여기 있는 규칙은 전부 실제 사고에서 나왔다:
  - 값은 반드시 **타이핑**으로 넣는다. WM_SETTEXT는 칸의 글자만 바꾸고
    대화상자 내부 상태(고른 파일/폴더)는 안 바꿔서, 저장은 앱의 기본 이름으로
    되고(2026-08-28) 열기는 샵마인이 빈 파일을 받는다(2026-08-27).
  - 타이핑은 글자가 유실되거나("Desktop"이 "Deskto"로, 2026-08-28) 사람이 그
    순간 다른 창을 누르면 엉뚱한 데로 샌다(2026-09-05). 넣은 뒤 되읽어 확인한다.
  - 확정은 Enter 대신 버튼을 BM_CLICK으로 누른다. 키 입력은 포커스가 있어야
    먹지만 BM_CLICK은 포커스·마우스와 무관하다 (winui.press_button).
"""

from __future__ import annotations

import time

from . import winui

VK_A = 0x41
VK_RETURN = 0x0D
# '파일 이름' 입력칸의 컨트롤 ID - 저장 대화상자는 1001, 열기 대화상자는 1148
# (2026-08-28 / 09-05 실측). ComboBoxEx32 > ComboBox 안에 있어 GetDlgItem으로는
# 못 잡고 하위 전체를 뒤져야 한다 (winui.find_descendants).
FILENAME_EDIT_IDS = (1001, 1148)


class DialogError(Exception):
    """대화상자에 경로를 넣거나 확정하지 못했다."""


def find_filename_edit(dlg_hwnd):
    """'파일 이름' 입력칸 핸들 (없으면 None)."""
    for edit in winui.find_descendants(dlg_hwnd, class_name="Edit"):
        if winui.u.GetDlgCtrlID(edit) in FILENAME_EDIT_IDS:
            return edit
    return None


def type_filename(dlg_hwnd, wanted: str) -> None:
    """대화상자를 앞으로 가져와 파일 이름 칸에 경로를 타이핑한다."""
    if not winui.bring_to_front(dlg_hwnd):
        raise DialogError("파일 대화상자를 앞으로 가져오지 못했습니다 - 다른 창이 앞을 막고 있습니다.")
    time.sleep(0.25)
    winui.ctrl_key(VK_A)  # 기본 파일명 전체 선택
    time.sleep(0.15)
    winui.type_text(wanted)
    time.sleep(0.3)


def fill_filename(dlg_hwnd, wanted: str, log=print, *, edit=None) -> str:
    """파일 이름 칸을 채우고, 실제로 들어간 값을 돌려준다 (다르면 최대 3번).

    edit: 이미 찾아둔 파일 이름 칸 핸들이 있으면 넘긴다.
    """
    edit = edit or find_filename_edit(dlg_hwnd)
    if edit is None:
        log("  파일 이름 입력칸을 찾지 못했습니다 - 검증 없이 타이핑합니다.")
        type_filename(dlg_hwnd, wanted)
        return wanted

    entered = ""
    for attempt in range(1, 4):
        type_filename(dlg_hwnd, wanted)
        entered = winui.ctrl_text(edit)
        if entered == wanted:
            return entered
        log(f"  입력값이 다릅니다({attempt}/3) - 다시 입력합니다: {entered!r}")
    return entered


def commit(dlg_hwnd, wanted: str, log=print) -> None:
    """[열기]/[저장] 버튼을 누른다. 누르기 직전에 파일 이름을 한 번 더 확인한다."""
    edit = find_filename_edit(dlg_hwnd)
    if edit is not None and winui.ctrl_text(edit) != wanted:
        log(f"  확정 직전 파일 이름이 바뀌어 있어 다시 넣습니다: {winui.ctrl_text(edit)!r}")
        if fill_filename(dlg_hwnd, wanted, log=log, edit=edit) != wanted:
            raise DialogError(
                f"확정 직전에 파일 이름을 되돌리지 못했습니다 (현재 값: {winui.ctrl_text(edit)!r}).")

    button, _ = winui.dlg_button(dlg_hwnd, winui.DLG_OK)
    if button:
        winui.press_button(button)
        return
    log("  확정 버튼을 찾지 못해 Enter로 확정합니다.")
    if not winui.bring_to_front(dlg_hwnd):
        raise DialogError("파일 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.2)
    winui.key(VK_RETURN)
