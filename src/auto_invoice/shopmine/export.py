"""3단계 - 샵마인에서 주문 목록 엑셀을 내보낸다.

실제 화면 흐름 (2026-08-27 실측):

    [배송중 탭] --Alt+X--> '엑셀 파일 생성' 창 (WebView2)
        --[엑셀 파일 생성] 클릭--> '다른 이름으로 저장' (#32770)
        --경로 입력 + Enter--> '엑셀파일을 여시겠습니까?' (#32770)
        --[아니요]--> 끝

'엑셀 파일 생성' 창만 WebView2(Chrome_WidgetWin_1)라 버튼이 HWND를 갖지
않는다. 그래서 그 버튼 하나만 색으로 찾아 클릭하고, 나머지 표준 대화상자는
전부 컨트롤 ID로 다룬다.

양식은 샵마인 쪽에 저장된 '송장 자동화' 프리셋을 쓴다. 그 양식이 수령인 /
마켓 주문번호 / 상품URL / 주문옵션 4개 컬럼을 내보내며, 이는
excel_io.read_pending_orders()가 요구하는 것과 정확히 일치한다.

다만 이 창은 **샵마인에 저장된 양식을 골라둔 채로 뜬다.** 사람이 다른 양식으로
한 번 쓰면 그 뒤로는 계속 그게 골라져 있고 (2026-08-28에 열어보니
'주문관리_마스터'였다), 그러면 컬럼이 달라 뒷단계가 통째로 어긋난다. 파일은
멀쩡히 만들어지기 때문에 바로 알아채기도 어렵다. 그래서 버튼을 누르기 전에
양식을 확인하고, 다르면 '송장 자동화'로 바꾼 뒤 바뀐 것까지 확인하고 넘어간다
(ensure_template). 글자를 읽어야 하는 이 한 곳만 msaa 로 읽는다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import msaa, winui

# [엑셀 파일 생성] 버튼의 보라파랑 (#5b63d3 계열)
EXPORT_BUTTON_COLOR = (91, 99, 211)

# 반드시 이 양식으로 내보내야 excel_io 가 읽을 수 있다.
TEMPLATE_NAME = "송장 자동화"
WEBVIEW_CLASS = "Chrome_WidgetWin_1"

VK_X = 0x58
VK_RETURN = 0x0D
VK_A = 0x41
VK_ESCAPE = 0x1B

SAVE_DIALOG_TITLE = "다른 이름으로 저장"
OPEN_ASK_TITLE = "질문"


class ExportError(RuntimeError):
    """내보내기 도중 안전하게 중단해야 하는 상황."""


def _webview(export_hwnd):
    """엑셀 생성 창 안에서 실제 화면을 그리는 WebView2 자식 창."""
    for k in winui.children(export_hwnd):
        if winui.class_of(k) == WEBVIEW_CLASS:
            return k
    return None


def _template_combo(webview_hwnd):
    """'엑셀 양식' 선택칸 요소. 값(accValue)이 지금 골라진 양식 이름이다."""
    root = msaa.from_window(webview_hwnd)
    if root is None:
        return None
    return msaa.find(root, lambda e: (msaa.role(e) == msaa.ROLE_COMBOBOX
                                      and (msaa.name(e) or "").startswith("엑셀 양식")))


def _template_value(webview_hwnd):
    combo = _template_combo(webview_hwnd)
    return None if combo is None else msaa.value(combo)


def _template_names(webview_hwnd):
    """펼쳐진 목록에 있는 양식 이름들. 안 읽힐 때가 많으니 오류 메시지 용도로만 쓴다."""
    combo = _template_combo(webview_hwnd)
    if combo is None:
        return []
    return [n for n in (msaa.name(e) for e in
                        msaa.find_all(combo, lambda e: msaa.role(e) == msaa.ROLE_LISTITEM))
            if n]


def _list_is_open(export_hwnd, probe):
    """양식 목록이 펼쳐져 있는지. 목록은 WebView2 프로세스의 '별도 팝업 창'이라,
    선택칸 바로 아래 지점이 엑셀 생성 창이 아닌 다른 창이면 펼쳐진 것이다."""
    at = winui.window_at(*probe)
    return bool(at) and not winui.is_descendant(at, export_hwnd)


def ensure_template(export_hwnd, template: str = TEMPLATE_NAME, timeout: float = 12.0,
                    log=print) -> bool:
    """'엑셀 양식'이 template 인지 확인하고, 아니면 그것으로 바꾼다.

    바꿨으면 True, 이미 맞아서 아무것도 안 했으면 False. 조금이라도 확실하지
    않으면 ExportError 로 멈춘다 - 엉뚱한 양식으로 내보내면 컬럼이 달라 뒷단계가
    통째로 어긋나는데, 파일 자체는 멀쩡히 만들어져서 알아채기 어렵다.

    고르는 방법은 '목록에서 해당 줄을 클릭'이 아니라 **이름을 타이핑**이다.
    목록이 펼쳐졌을 때 이름을 치면 앞글자가 맞는 줄로 옮겨가고, 그 순간
    선택칸의 값이 바로 바뀐다. 목록 줄의 좌표는 읽히지 않을 때가 많지만
    (msaa 주석 참고) 값은 항상 읽히므로, 이 방법은 확인까지 확실하다.
    """
    wv = _webview(export_hwnd)
    if wv is None:
        raise ExportError("엑셀 생성 창에서 화면 영역(WebView2)을 찾지 못했습니다.")

    combo = _template_combo(wv)
    if combo is None:
        raise ExportError("'엑셀 양식' 선택칸을 찾지 못했습니다 - 화면 구성이 바뀐 것 같습니다.")

    current = msaa.value(combo)
    if current == template:
        log(f"  엑셀 양식 확인: '{template}' - 그대로 사용")
        return False
    log(f"  엑셀 양식이 '{current}' 입니다 - '{template}'(으)로 바꿉니다.")

    box = msaa.location(combo)
    if box is None or box[2] <= 0 or box[3] <= 0:
        raise ExportError("'엑셀 양식' 선택칸의 위치를 읽지 못했습니다.")
    r = winui.rect(export_hwnd)
    spot = (box[0] + box[2] // 2, box[1] + box[3] // 2)
    probe = (spot[0], box[1] + box[3] + 20)     # 선택칸 바로 아래 = 목록이 펼쳐지는 자리

    # 1) 선택칸을 눌러 목록을 편다. 펼쳐지지 않았다면 타이핑이 엉뚱한 곳으로
    #    가므로 여기서 멈춘다.
    ok, msg = winui.safe_click(export_hwnd, (spot[0] - r.left, spot[1] - r.top),
                               label="엑셀 양식 목록 열기")
    if not ok:
        raise ExportError(msg)
    end = time.time() + 5.0
    while time.time() < end and not _list_is_open(export_hwnd, probe):
        time.sleep(0.2)
    if not _list_is_open(export_hwnd, probe):
        raise ExportError("엑셀 양식 목록이 펼쳐지지 않았습니다.")

    # 2) 이름을 쳐서 그 줄로 옮긴다. 아직 확정(Enter)하기 전에 값부터 확인한다 -
    #    이름이 조금 다른 양식으로 옮겨갔을 수 있기 때문이다.
    winui.type_text(template)
    end = time.time() + timeout
    while time.time() < end:
        if _template_value(wv) == template:
            break
        time.sleep(0.3)
    now = _template_value(wv)
    if now != template:
        winui.key(VK_ESCAPE)
        names = _template_names(wv)
        raise ExportError(
            f"'{template}' 양식을 고르지 못했습니다 (선택칸 값: '{now}'). "
            + (f"샵마인에 있는 양식: {names}" if names else
               "그런 이름의 양식이 샵마인에 없는 것 같습니다."))

    # 3) Enter 로 확정하고 목록을 닫는다. 목록이 열린 채로 두면 다음 단계의
    #    [엑셀 파일 생성] 클릭이 '목록 닫기'로 먹혀버린다.
    for _ in range(2):
        if not _list_is_open(export_hwnd, probe):
            break
        winui.key(VK_RETURN)
        time.sleep(0.6)
    if _list_is_open(export_hwnd, probe):
        winui.key(VK_ESCAPE)
        raise ExportError("엑셀 양식 목록이 닫히지 않았습니다.")

    final = _template_value(wv)
    if final != template:
        raise ExportError(f"엑셀 양식을 '{template}'(으)로 바꾸지 못했습니다 (지금 값: '{final}').")
    log(f"  엑셀 양식 변경 완료: '{template}'")
    return True


# 표준 '다른 이름으로 저장' 대화상자의 '파일 이름' 입력칸 컨트롤 ID.
# ComboBoxEx32 안에 들어있어서 GetDlgItem으로는 못 잡고, 하위 전체를 뒤져야 한다.
FILENAME_EDIT_ID = 1001


def _find_filename_edit(save_hwnd):
    """저장 대화상자의 '파일 이름' 입력칸 핸들 (없으면 None)."""
    edits = winui.find_descendants(save_hwnd, class_name="Edit", ctrl_id=FILENAME_EDIT_ID)
    return edits[0] if edits else None


def _wait_for_default_filename(edit, timeout: float = 6.0) -> str:
    """샵마인이 기본 파일명을 채워 넣고 잠잠해질 때까지 기다린다.

    창이 뜬 직후에 우리 경로를 넣으면 **그 뒤에 앱이 기본 파일명으로 덮어쓴다**.
    실제로 이것 때문에, 우리가 넣고 검증까지 마친 경로가 저장 시점에는 앱의
    기본 이름으로 바뀌어 있었다(2026-08-28: 우리가 기다린 이름 대신
    "주문목록-선택-송장 자동화(...).xls"로 저장돼서, 파일은 만들어졌는데도
    "생성되지 않았습니다"로 중단됐다).

    그래서 값이 비어 있지 않고 연속으로 같게 읽힐 때까지(=앱이 다 채웠을 때까지)
    기다린 다음에 우리 값을 넣는다.
    """
    end = time.time() + timeout
    last, stable = None, 0
    while time.time() < end:
        current = winui.ctrl_text(edit)
        if current and current == last:
            stable += 1
            if stable >= 2:
                return current
        else:
            stable = 0
        last = current
        time.sleep(0.25)
    return last or ""


def _fill_save_filename(save_hwnd, wanted: str, log=print) -> str:
    """저장 대화상자의 '파일 이름' 칸을 채우고, 실제로 들어간 값을 돌려준다.

    반드시 '타이핑'으로 채운다. WM_SETTEXT로 값을 넣으면 입력칸 글자만 바뀌고
    대화상자 내부 상태에는 반영되지 않아서, 저장할 때 앱의 기본 파일명과
    기본 폴더가 그대로 쓰인다(2026-08-28 실측: 우리가 넣은 경로는 무시되고
    바탕화면에 "주문목록-선택-송장 자동화(...).xls"로 저장됐다).

    타이핑은 글자가 유실될 수 있어서(같은 날 "Desktop"이 "Deskto"로 들어가
    저장이 통째로 실패했다) 넣은 뒤 반드시 되읽어 검증하고, 다르면 다시 넣는다.
    이 두 사고가 이 함수가 존재하는 이유다 - 어느 쪽도 조용히 넘어가면 안 된다.
    """
    edit = _find_filename_edit(save_hwnd)
    if edit is None:
        log("  파일 이름 입력칸을 찾지 못했습니다 - 검증 없이 타이핑합니다.")
        _type_filename(save_hwnd, wanted)
        return wanted

    default_name = _wait_for_default_filename(edit)
    if default_name:
        log(f"  앱이 넣은 기본 파일명 확인: {default_name}")

    entered = ""
    for attempt in range(1, 4):
        _type_filename(save_hwnd, wanted)
        entered = winui.ctrl_text(edit)
        if entered == wanted:
            return entered
        log(f"  입력값이 다릅니다({attempt}/3) - 다시 입력합니다: {entered!r}")
    return entered


def _type_filename(save_hwnd, wanted: str) -> None:
    """대화상자를 앞으로 가져와 파일 이름 칸에 경로를 타이핑한다."""
    if not winui.bring_to_front(save_hwnd):
        raise ExportError("저장 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.4)
    winui.ctrl_key(VK_A)            # 기본 파일명 전체 선택
    time.sleep(0.2)
    winui.type_text(wanted)
    time.sleep(0.4)


def _commit_save_dialog(save_hwnd, wanted: str, log=print) -> None:
    """저장 버튼을 누른다. 누르기 직전에 파일 이름을 한 번 더 확인한다.

    Enter 키 대신 [저장] 버튼을 BM_CLICK으로 누른다 - 키 입력은 대화상자가
    앞에 있어야 먹지만 BM_CLICK은 포커스와 무관하고, 마우스도 쓰지 않는다.
    """
    edit = _find_filename_edit(save_hwnd)
    if edit is not None:
        current = winui.ctrl_text(edit)
        if current != wanted:
            log(f"  저장 직전 파일 이름이 바뀌어 있어 다시 넣습니다: {current!r}")
            _type_filename(save_hwnd, wanted)
            current = winui.ctrl_text(edit)
            if current != wanted:
                raise ExportError(
                    f"저장 직전에 파일 이름을 되돌리지 못했습니다 (현재 값: {current!r})."
                )

    button, _ = winui.dlg_button(save_hwnd, winui.DLG_OK)
    if button:
        winui.press_button(button)
        return
    log("  [저장] 버튼을 찾지 못해 Enter로 저장합니다.")
    if not winui.bring_to_front(save_hwnd):
        raise ExportError("저장 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.3)
    winui.key(VK_RETURN)


def _nearby_new_files_hint(target: Path, since: float) -> str:
    """기대한 이름이 없을 때, 같은 폴더에 방금 생긴 다른 파일을 알려준다.

    앱이 우리가 넣은 경로를 무시하고 자기 기본 파일명으로 저장해버린 적이
    있는데(2026-08-28), 그때 메시지가 "생성되지 않았습니다"뿐이라 파일이
    멀쩡히 바탕화면에 있는데도 원인을 알 수 없었다.
    """
    try:
        recent = [
            f for f in target.parent.iterdir()
            if f.is_file() and f.suffix.lower() in (".xls", ".xlsx") and f.stat().st_mtime >= since
        ]
    except OSError:
        return ""
    if not recent:
        return ""
    names = ", ".join(f.name for f in sorted(recent, key=lambda f: f.stat().st_mtime, reverse=True)[:3])
    return (
        f" - 다만 같은 폴더에 방금 만들어진 파일이 있습니다: {names}. "
        "샵마인이 우리가 넣은 파일명을 무시하고 기본 이름으로 저장했을 수 있습니다."
    )


def _commit_save_dialog(save_hwnd, wanted: str, log=print) -> None:
    """저장 버튼을 누른다. 누르기 직전에 파일 이름을 한 번 더 확인한다.

    Enter 키 대신 [저장] 버튼을 BM_CLICK으로 누른다 - 키 입력은 대화상자가
    앞에 있어야 먹지만 BM_CLICK은 포커스와 무관하고, 마우스도 쓰지 않는다.
    """
    edit = _find_filename_edit(save_hwnd)
    if edit is not None:
        current = winui.ctrl_text(edit)
        if current != wanted:
            log(f"  저장 직전 파일 이름이 바뀌어 있어 다시 넣습니다: {current!r}")
            if not winui.set_ctrl_text(edit, wanted):
                raise ExportError(
                    f"저장 직전에 파일 이름을 되돌리지 못했습니다 (현재 값: {current!r})."
                )

    button, _ = winui.dlg_button(save_hwnd, winui.DLG_OK)
    if button:
        winui.press_button(button)
        return
    log("  [저장] 버튼을 찾지 못해 Enter로 저장합니다.")
    if not winui.bring_to_front(save_hwnd):
        raise ExportError("저장 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.3)
    winui.key(VK_RETURN)


def export_to(target_path, tab_title: str = "배송중", timeout: float = 40.0,
              log=print) -> Path:
    """샵마인 현재 탭의 주문 목록을 target_path 로 내보낸다.

    각 단계마다 기대한 창이 뜨는지 확인하고, 아니면 즉시 멈춘다.
    화면이 예상과 다를 때 계속 클릭하는 것이 제일 위험하기 때문이다.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    tmp_shot = str(target.parent / "_export_probe.bmp")

    # 1) 메인 창 확보
    mains = winui.find_windows(title_startswith="ShopMine::")
    if not mains:
        raise ExportError("샵마인이 실행 중이지 않습니다.")
    main_hwnd = mains[0][0]
    if not winui.bring_to_front(main_hwnd):
        raise ExportError("샵마인 창을 앞으로 가져오지 못했습니다.")
    time.sleep(0.5)
    log(f"  샵마인 창 확보 (탭: {tab_title})")

    # 2) Alt+X -> 엑셀 파일 생성 창
    winui.key(VK_X, alt=True)
    win = winui.wait_for_window(title_equals=tab_title, timeout=timeout)
    if win is None:
        raise ExportError(
            f"Alt+X 후 '{tab_title}' 엑셀 생성 창이 뜨지 않았습니다. "
            "다른 탭에 있거나 화면 상태가 다를 수 있습니다.")
    export_hwnd = win[0]
    log(f"  엑셀 파일 생성 창 확인 (hwnd={export_hwnd})")

    # 3) 엑셀 양식이 '송장 자동화'인지 확인 (아니면 바꾼다)
    winui.bring_to_front(export_hwnd)
    time.sleep(0.6)
    ensure_template(export_hwnd, log=log)

    # 4) [엑셀 파일 생성] 버튼을 색으로 찾아 클릭
    found = winui.locate_button_by_color(export_hwnd, EXPORT_BUTTON_COLOR, tmp_shot)
    if found is None:
        raise ExportError("'엑셀 파일 생성' 버튼(보라파랑)을 화면에서 찾지 못했습니다.")
    center, box, npx = found
    log(f"  버튼 발견: 상대좌표 {center} (영역 {box}, {npx}px)")

    ok, msg = winui.safe_click(export_hwnd, center,
                               expect_color=EXPORT_BUTTON_COLOR, tol=60,
                               label="엑셀 파일 생성")
    log(f"  {msg}")
    if not ok:
        raise ExportError(msg)

    # 5) 저장 대화상자 -> 경로 입력
    dlg = winui.wait_for_window(title_equals=SAVE_DIALOG_TITLE, timeout=timeout)
    if dlg is None:
        raise ExportError("'다른 이름으로 저장' 창이 뜨지 않았습니다.")
    save_hwnd = dlg[0]
    if not winui.bring_to_front(save_hwnd):
        raise ExportError("저장 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.4)

    wanted = str(target).replace("/", "\\")
    entered = _fill_save_filename(save_hwnd, wanted, log=log)
    if entered != wanted:
        raise ExportError(
            f"저장 경로가 정확히 입력되지 않았습니다. 입력하려던 값: {wanted!r} / "
            f"실제로 들어간 값: {entered!r}"
        )
    _commit_save_dialog(save_hwnd, wanted, log=log)
    log(f"  저장 경로 입력: {target}")

    # 6) '엑셀파일을 여시겠습니까?' -> 아니요
    ask = winui.wait_for_window(title_equals=OPEN_ASK_TITLE, timeout=15.0)
    if ask is not None:
        ok, msg = winui.click_dlg_button(ask[0], winui.DLG_NO, label="엑셀 열기 묻기")
        log(f"  {msg}")

    # 7) 결과 확인
    started_at = time.time() - 120  # 이 실행에서 만들어졌다고 볼 시간 범위
    end = time.time() + timeout
    while time.time() < end:
        if target.exists() and target.stat().st_size > 0:
            break
        time.sleep(0.5)
    else:
        raise ExportError(
            f"엑셀 파일이 생성되지 않았습니다: {target}{_nearby_new_files_hint(target, started_at)}"
        )

    if winui.find_windows(title_equals=SAVE_DIALOG_TITLE):
        raise ExportError("저장 대화상자가 아직 열려 있습니다 - 경로가 거부됐을 수 있습니다.")

    try:
        os.remove(tmp_shot)
    except OSError:
        pass

    log(f"  엑셀 생성 완료: {target} ({target.stat().st_size:,} bytes)")
    return target
