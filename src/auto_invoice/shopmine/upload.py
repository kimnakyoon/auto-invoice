"""배송중 탭에서 대상 주문을 고르고, CSV를 올려, 쇼핑몰까지 반영한다.

실제 화면 흐름 (2026-08-27 실측 검증):

    [배송중 탭]
      --수집 결과 필터에 '경동택배' + [수집결과내 검색]--> 그 택배사 주문만 남음
      --[송장수정모드 켜기]--> [송장번호수정(S)] / [발송정보일괄등록(수정용)(U)]
          등장 + 그리드 앞에 '택배사(수정용)/송장번호(수정용)' 컬럼 추가
      --헤더 [전체선택] 체크--> 필터에 걸린 행 전부 체크 (필터를 바꿔도 유지됨)
      --필터 '직접' + 전체선택--> 직접전달 건도 함께 체크
      --필터초기화(F8) --> [엑셀파일생성](Alt+X): 체크된 행만 내보내진다
      --Alt+U--> '발송정보일괄등록' 창 (순수 WinForms, 컨트롤 전부 HWND 있음)
      --경로 EDIT 에 CSV 경로 설정 + [일괄등록(&S)]-->
          '발송정보일괄등록 결과' 창 ... 그리드의 '송장번호(수정용)' 컬럼이 채워짐
      --송장번호(수정용)가 채워진 행만 체크 --> [송장번호수정] 클릭
      --'선택한 N개의 주문을 [송장번호수정] 하시겠습니까?' --[예]-->
          '송장번호수정 결과' 창 ('오류없음.' 이면 성공)
      --오류가 없으면 [송장수정모드 끄기]

안전장치의 핵심은 마지막 확인 대화상자다. 이 창이 '선택한 N개'라고 건수를
알려주므로, N이 기대와 다르면 [예] 대신 [아니요]를 눌러 아무것도 반영하지
않고 멈출 수 있다. 화면이 예상과 다를 때 그대로 진행하는 것이 가장 위험하다.

'발송정보일괄등록 결과' 창은 처리가 끝나도 라벨이 "처리중입니다."에서 안
바뀐다. 이 문구로 완료를 판단하면 안 된다 - 실제 반영 여부는 그리드의
'송장번호(수정용)' 컬럼으로 확인해야 한다(grid.py).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import grid, winui

VK_A = 0x41
VK_U = 0x55
VK_RETURN = 0x0D
VK_F8 = 0x77

UPLOAD_WINDOW = "발송정보일괄등록"
OPEN_DIALOG_TITLE = "열기"
UPLOAD_RESULT_WINDOW = "발송정보일괄등록 결과"
APPLY_RESULT_WINDOW = "송장번호수정 결과"
CONFIRM_WINDOW = "질문"

# 창 상대좌표 (배송중 탭). 좌표에 의존하는 곳은 여기뿐이며,
# 클릭 전에 winui.safe_click 이 가림/커서도달을 검증한다.
REL_EDIT_MODE_TOGGLE = (77, 285)     # [송장수정모드 켜기/끄기]
REL_APPLY_BUTTON = (202, 285)        # [송장번호수정(S)]
SEARCH_EDIT_HINT = (98, 210)         # 수집 결과 필터 입력란
FILTER_STATUS_HINT = (495, 222)      # '필터안됨.' / '"xxx"(으)로 필터됨.' 라벨
BULK_INPUT_EDIT_HINT = (599, 309)    # 송장수정모드에서만 나타나는 '송장번호 일괄입력' 칸

# [송장번호수정]은 실제로 쇼핑몰에 접속해 송장을 등록한다. 그동안 '쇼핑몰 연결'
# 창이 뜨고 그리드가 잠기는데, 이를 기다리지 않고 다음 조작을 하면 클릭이
# 먹지 않는다. 8건 중 5건이 처음에 이 이유로 반영되지 않았다.
#
# 주의: 1단계에서 연결 상태를 확인할 때 여는 창(connect.py)도 제목이 똑같다.
# 그래서 connect 는 확인이 끝나면 그 창을 반드시 닫는다 - 열어둔 채로 두면
# 아래 wait_until_idle 이 '아직 연결 중'으로 오해한다.
BUSY_WINDOW = "쇼핑몰 연결"

# 송장을 고쳐야 하는 주문의 '수정 전 택배사'. 샵마인이 이 두 값일 때만
# 송장번호(수정용)를 비워두므로, 목록을 고르는 기준이자 반영 여부의 근거다.
TARGET_COURIERS = ("경동택배", "직접")


class UploadError(RuntimeError):
    """업로드/반영 도중 안전하게 중단해야 하는 상황."""


def wait_until_idle(timeout: float = 300.0, log=print) -> bool:
    """샵마인이 쇼핑몰 연결/처리를 끝낼 때까지 기다린다."""
    end = time.time() + timeout
    waited = False
    while time.time() < end:
        if not winui.find_windows(title_startswith=BUSY_WINDOW):
            if waited:
                log("  쇼핑몰 연결 완료")
            return True
        if not waited:
            log("  쇼핑몰 연결 진행 중 - 끝날 때까지 대기")
            waited = True
        time.sleep(2.0)
    log(f"  경고: {timeout:.0f}초를 기다렸는데도 '{BUSY_WINDOW}'이 끝나지 않았습니다.")
    return False


def main_window():
    wins = winui.find_windows(title_startswith="ShopMine::")
    if not wins:
        raise UploadError("샵마인이 실행 중이지 않습니다.")
    return wins[0][0]


def _rect(hwnd):
    return winui.rect(hwnd)


def _near(hwnd, base, hint, tol_x=25, tol_y=20) -> bool:
    r = _rect(hwnd)
    return (abs((r.left - base.left) - hint[0]) < tol_x
            and abs((r.top - base.top) - hint[1]) < tol_y
            and winui.u.IsWindowVisible(hwnd))


def _click_control(hwnd, owner, label):
    """HWND를 가진 버튼을 BM_CLICK으로 누른다 (커서를 쓰지 않는다).

    좌표 클릭이면 실행 중 사람이 마우스를 건드릴 때마다 중단된다. 컨트롤이
    HWND를 갖는 곳에서는 그럴 이유가 없다. 각 호출부가 결과(창이 뜨는지,
    필터 라벨이 바뀌는지)로 눌렸는지 확인하므로 클릭 자체를 검증하진 않는다.
    """
    winui.bring_to_front(owner)
    time.sleep(0.35)
    winui.press_button(hwnd)


def _close_window(title, log=print):
    for h, _t, _r in winui.find_windows(title_equals=title):
        btn = winui.find_child(h, "BUTTON",
                               lambda k: winui.ctrl_text(k).startswith("닫기"))
        if btn is None:
            continue
        _click_control(btn, h, f"{title} 닫기")
        if winui.wait_for_window_gone(title_equals=title, timeout=8.0):
            log(f"  '{title}' 닫음")
        else:
            log(f"  경고: '{title}' 창이 닫히지 않았습니다.")


# --- 송장수정모드 ---------------------------------------------------
# 툴바 버튼은 HWND를 갖지 않아 글자를 읽을 수 없다. 대신 송장수정모드에서만
# 나타나는 '송장번호 일괄입력(수정용)' 입력란(EDIT)이 있는지로 판정한다.
# 예전에는 Alt+U 로 업로드 창이 열리는지 봤는데, 확인만 하려고 창을 여닫는
# 부작용이 있었다.

def edit_mode_on(main_hwnd=None) -> bool:
    """송장수정모드가 켜져 있는가."""
    main_hwnd = main_hwnd or main_window()
    base = _rect(main_hwnd)
    return winui.find_child(
        main_hwnd, "EDIT", lambda k: _near(k, base, BULK_INPUT_EDIT_HINT)) is not None


def _toggle_edit_mode(main_hwnd, label):
    if not winui.bring_to_front(main_hwnd):
        raise UploadError("샵마인 창을 앞으로 가져오지 못했습니다.")
    time.sleep(0.5)
    ok, msg = winui.safe_click(main_hwnd, REL_EDIT_MODE_TOGGLE, label=label)
    if not ok:
        raise UploadError(msg)
    time.sleep(2.0)
    return msg


def ensure_edit_mode(log=print) -> None:
    """송장수정모드를 켠다 (이미 켜져 있으면 그대로 둔다)."""
    main = main_window()
    if edit_mode_on(main):
        log("  송장수정모드: 이미 켜져 있음")
        return
    log(f"  {_toggle_edit_mode(main, '송장수정모드 켜기')}")
    if not edit_mode_on(main):
        raise UploadError(
            "[송장수정모드 켜기]를 눌렀는데 모드가 켜지지 않았습니다. "
            "배송중 탭이 아니거나 화면 상태가 다를 수 있습니다.")
    log("  송장수정모드 켜짐 확인")


def disable_edit_mode(log=print) -> None:
    """송장수정모드를 끈다 (모두 정상 반영된 뒤 마무리)."""
    main = main_window()
    if not edit_mode_on(main):
        log("  송장수정모드: 이미 꺼져 있음")
        return
    log(f"  {_toggle_edit_mode(main, '송장수정모드 끄기')}")
    if edit_mode_on(main):
        raise UploadError("[송장수정모드 끄기]를 눌렀는데 모드가 꺼지지 않았습니다.")
    log("  송장수정모드 꺼짐 확인")


# --- 수집 결과 필터 -------------------------------------------------

def _search_edit(main_hwnd):
    base = _rect(main_hwnd)
    return winui.find_child(main_hwnd, "EDIT",
                            lambda k: _near(k, base, SEARCH_EDIT_HINT, 40, 40))


def _search_button(main_hwnd):
    base = _rect(main_hwnd)

    def hit(k):
        return (winui.ctrl_text(k).startswith("수집결과내 검색")
                and winui.u.IsWindowVisible(k)
                and _rect(k).left - base.left > 0)

    return winui.find_child(main_hwnd, "BUTTON", hit)


def filter_status(main_hwnd) -> str:
    """샵마인이 표시하는 현재 필터 상태 문구를 읽는다.

    '필터안됨.' 또는 '" 12345"(으)로 필터됨.' 형태다. 이 문구가 필터가
    실제로 걸렸는지를 확인할 수 있는 유일하게 확실한 근거다 - 그리드 셀은
    HWND가 없어 읽을 수 없기 때문이다.

    주의: 필터초기화(F8) 뒤에는 이 라벨이 갱신되지 않고 직전 문구가 그대로
    남는다. 초기화 확인용으로는 쓸 수 없다.
    """
    base = _rect(main_hwnd)
    for k in winui.children(main_hwnd):
        if not winui.u.IsWindowVisible(k) or winui.class_of(k) != "STATIC":
            continue
        r = _rect(k)
        rel = (r.left - base.left, r.top - base.top)
        if abs(rel[0] - FILTER_STATUS_HINT[0]) < 220 and abs(rel[1] - FILTER_STATUS_HINT[1]) < 30:
            tx = winui.ctrl_text(k)
            if "필터" in tx:
                return tx.strip()
    return ""


def set_filter(keyword, log=print) -> None:
    """수집 결과 필터로 목록을 좁힌다.

    입력란에 값만 넣고 Enter를 치면 포커스가 없어 필터가 걸리지 않는다
    (실제로 '경동택배'가 조용히 무시됐다). [수집결과내 검색] 버튼을 직접
    누른다 - 이 버튼은 HWND가 있어 좌표에 의존하지 않는다.

    필터가 실제로 걸렸는지 반드시 확인한다. 필터가 안 걸린 채로 전체선택을
    하면 목록 전체가 체크되는데, 건수 검증만으로는 막을 수 없다.
    """
    main = main_window()
    winui.bring_to_front(main)
    time.sleep(0.4)
    edit = _search_edit(main)
    if edit is None:
        raise UploadError("수집 결과 필터 입력란을 찾지 못했습니다.")
    button = _search_button(main)
    if button is None:
        raise UploadError("[수집결과내 검색] 버튼을 찾지 못했습니다.")

    if not winui.set_ctrl_text(edit, keyword):
        raise UploadError(f"필터 입력란에 '{keyword}'가 들어가지 않았습니다.")
    time.sleep(0.3)
    _click_control(button, main, "수집결과내 검색")
    time.sleep(2.5)

    status = filter_status(main)
    if keyword not in status:
        raise UploadError(
            f"필터가 '{keyword}'로 걸리지 않았습니다 (화면 표시: {status or '(읽을 수 없음)'}). "
            "이대로 진행하면 엉뚱한 주문이 선택될 수 있어 중단합니다.")
    log(f"  목록 필터: {status}")


def reset_filter(log=print) -> None:
    """필터초기화(F8). 체크해둔 행은 그대로 남는다."""
    main = main_window()
    winui.bring_to_front(main)
    time.sleep(0.4)
    edit = _search_edit(main)
    if edit is not None:
        winui.set_ctrl_text(edit, "")
        time.sleep(0.2)
    winui.bring_to_front(main)
    time.sleep(0.3)
    winui.key(VK_F8)
    time.sleep(2.5)
    log("  목록 필터 초기화 (F8)")


# --- 2단계: 반영할 주문 고르기 --------------------------------------

def select_target_orders(keywords, log=print) -> int:
    """송장을 고쳐야 할 주문(택배사가 경동택배/직접전달인 건)을 체크한다.

    키워드마다 필터를 걸고 헤더 [전체선택]을 누른다. 체크 상태는 필터를 바꿔도,
    필터를 초기화해도 유지되므로 마지막에 초기화하면 두 그룹이 모두 체크된
    상태로 남는다.
    """
    main = main_window()
    total = 0
    for keyword in keywords:
        log(f"  '{keyword}' 검색")
        set_filter(keyword, log=log)
        total += grid.check_all_filtered(main, log=log)
    reset_filter(log=log)
    if total == 0:
        raise UploadError(
            f"{'/'.join(keywords)} 에 해당하는 주문이 하나도 없습니다. "
            "고칠 송장이 없으니 아무것도 하지 않고 멈춥니다.")
    return total


# --- 5단계: CSV 업로드 ----------------------------------------------

def _open_upload_window(main_hwnd):
    """Alt+U 로 '발송정보일괄등록' 창을 연다.

    Alt+U 는 '지금 포커스를 가진 창'으로 간다. 공급사 조회 단계에서 브라우저가
    떠 있으면 샵마인이 앞으로 나오지 못한 채 단축키가 브라우저로 새고, 창이
    안 열려서 멈춘다 (실제로 그렇게 실패했다). 그래서 앞으로 가져오는 데
    성공했는지를 반드시 확인하고, 한 번 더 시도한다.
    """
    last = ""
    for attempt in (1, 2, 3):
        if not winui.bring_to_front(main_hwnd):
            last = ("샵마인 창을 앞으로 가져오지 못했습니다 - 다른 창(브라우저 등)이 "
                    "앞을 막고 있습니다.")
            time.sleep(1.5)
            continue
        time.sleep(0.8)
        winui.key(VK_U, alt=True)
        found = winui.wait_for_window(title_equals=UPLOAD_WINDOW, timeout=15.0)
        if found is not None:
            return found[0]
        last = ("Alt+U 후 '발송정보일괄등록' 창이 열리지 않았습니다. "
                "송장수정모드가 꺼져 있거나 배송중 탭이 아닐 수 있습니다.")
    raise UploadError(last)


def _path_edit(upload_hwnd):
    def hit(k):
        if winui.ctrl_text(winui.u.GetParent(k)) != "엑셀 파일 열기":
            return False
        r = winui.rect(k)
        return r.right - r.left > 200
    return winui.find_child(upload_hwnd, "EDIT", hit)


def _pick_csv_file(upload_hwnd, csv_str, log=print) -> None:
    """[찾아보기]로 CSV 파일을 고른다.

    경로 입력란에 WM_SETTEXT 로 값만 넣으면 **화면에는 제대로 보이는데
    [일괄등록]은 0건을 처리한다.** 샵마인이 그 텍스트가 아니라 '대화상자로
    고른 파일'을 쓰기 때문이다. 결과 창도 빈 채로 "준비."만 뜨고 오류조차
    알려주지 않아서, 3건이 조용히 반영되지 않은 걸 그리드를 보고서야 알았다.
    그래서 반드시 대화상자를 거친다.
    """
    browse = winui.find_child(upload_hwnd, "BUTTON",
                              lambda k: winui.ctrl_text(k).startswith("찾아보기"))
    if browse is None:
        raise UploadError("[찾아보기] 버튼을 찾지 못했습니다.")
    winui.bring_to_front(upload_hwnd)
    time.sleep(0.4)
    winui.press_button(browse)

    dlg = winui.wait_for_window(title_equals=OPEN_DIALOG_TITLE, timeout=15.0)
    if dlg is None:
        raise UploadError(f"[찾아보기] 후 '{OPEN_DIALOG_TITLE}' 대화상자가 뜨지 않았습니다.")
    if not winui.bring_to_front(dlg[0]):
        raise UploadError("파일 선택 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.5)
    winui.ctrl_key(VK_A)
    time.sleep(0.2)
    winui.type_text(csv_str)
    time.sleep(0.4)
    winui.key(VK_RETURN)

    if not winui.wait_for_window_gone(title_equals=OPEN_DIALOG_TITLE, timeout=15.0):
        raise UploadError(
            f"파일 선택 대화상자가 닫히지 않았습니다 - 경로가 거부됐을 수 있습니다: {csv_str}")

    edit = _path_edit(upload_hwnd)
    shown = winui.ctrl_text(edit) if edit is not None else ""
    if csv_str not in shown:
        raise UploadError(f"고른 파일이 경로칸에 반영되지 않았습니다 (화면: {shown!r}).")
    log(f"  파일 선택: {csv_str}")


def bulk_register(csv_path, log=print) -> None:
    """CSV를 발송정보일괄등록(수정용)으로 올리고 [일괄등록]까지 누른다."""
    csv_file = Path(csv_path).resolve()
    if not csv_file.exists():
        raise UploadError(f"CSV 파일이 없습니다: {csv_file}")
    csv_str = str(csv_file).replace("/", "\\")

    main = main_window()
    existing = winui.find_windows(title_equals=UPLOAD_WINDOW)
    if existing:
        upload_hwnd = existing[0][0]
    else:
        upload_hwnd = _open_upload_window(main)
    log(f"  발송정보일괄등록 창 확인 (hwnd={upload_hwnd})")

    _pick_csv_file(upload_hwnd, csv_str, log=log)

    submit = winui.find_child(upload_hwnd, "BUTTON",
                              lambda k: winui.ctrl_text(k).startswith("일괄등록"))
    if submit is None:
        raise UploadError("[일괄등록] 버튼을 찾지 못했습니다.")

    _click_control(submit, upload_hwnd, "일괄등록")
    log("  [일괄등록] 클릭")

    if winui.wait_for_window(title_equals=UPLOAD_RESULT_WINDOW, timeout=60.0) is None:
        raise UploadError("'발송정보일괄등록 결과' 창이 뜨지 않았습니다.")
    time.sleep(2.0)
    log("  일괄등록 결과 창 확인")

    _close_window(UPLOAD_RESULT_WINDOW, log)
    _close_window(UPLOAD_WINDOW, log)


# --- 6단계: 쇼핑몰까지 반영 -----------------------------------------

def select_registered_rows(log=print) -> int:
    """일괄등록으로 송장번호(수정용)가 채워진 행만 체크한다.

    송장수정모드에서 이 컬럼은 원래 값으로 미리 채워져 있고 경동택배·직접전달
    행만 비어 있다. 그 두 필터 안에서 '채워짐'은 곧 '이번 일괄등록이 반영됨'을
    뜻한다 - 조회에 실패해 CSV에 없던 건은 여전히 비어 있다.
    """
    main = main_window()
    if not edit_mode_on(main):
        raise UploadError(
            "송장수정모드가 꺼져 있습니다. 그 상태에서는 '송장번호(수정용)' 컬럼이 "
            "화면에 없어 어떤 행이 반영됐는지 알 수 없습니다.")
    grid.clear_all_checks(main, log=log)
    total = 0
    for keyword in TARGET_COURIERS:
        set_filter(keyword, log=log)
        n = grid.check_filled_rows(main, log=log)
        log(f"  '{keyword}' 에서 송장이 채워진 {n}건 체크")
        total += n
    reset_filter(log=log)
    if total == 0:
        raise UploadError(
            "송장번호(수정용)가 채워진 행을 하나도 찾지 못했습니다. "
            "일괄등록이 실제로 반영되지 않았을 수 있어 중단합니다.")
    return total


def apply_tracking(expected_count: int, log=print, close_result: bool = True) -> str:
    """체크해둔 행을 [송장번호수정]으로 쇼핑몰까지 반영한다.

    확인 대화상자가 알려주는 건수가 expected_count 와 다르면 [아니요]를 눌러
    아무것도 반영하지 않고 UploadError 를 낸다. 이것이 이 파이프라인 전체의
    마지막이자 가장 중요한 안전장치다.
    """
    main = main_window()
    winui.bring_to_front(main)
    time.sleep(0.5)

    ok, msg = winui.safe_click(main, REL_APPLY_BUTTON, label="송장번호수정")
    log(f"  {msg}")
    if not ok:
        raise UploadError(msg)

    dlg = winui.wait_for_window(title_equals=CONFIRM_WINDOW, timeout=15.0)
    if dlg is None:
        raise UploadError(
            "[송장번호수정] 확인 대화상자가 뜨지 않았습니다. "
            "선택된 행이 없거나 화면이 갱신 중일 수 있습니다.")
    confirm_hwnd = dlg[0]

    message = ""
    for k in winui.children(confirm_hwnd):
        tx = winui.ctrl_text(k)
        if "주문을" in tx or "송장번호수정" in tx:
            message = tx
            break
    log(f"  확인 문구: {message!r}")

    m = re.search(r"선택한\s*([\d,]+)\s*개", message)
    if not m:
        winui.click_dlg_button(confirm_hwnd, winui.DLG_NO, label="확인")
        raise UploadError(f"확인 문구에서 건수를 읽지 못해 중단했습니다: {message!r}")

    count = int(m.group(1).replace(",", ""))
    if count != expected_count:
        winui.click_dlg_button(confirm_hwnd, winui.DLG_NO, label="확인")
        raise UploadError(
            f"반영 대상 건수가 예상과 다릅니다 (화면 {count}건 / 예상 {expected_count}건). "
            "[아니요]를 눌러 아무것도 반영하지 않았습니다.")

    ok, msg = winui.click_dlg_button(confirm_hwnd, winui.DLG_YES, label="송장번호수정 확인")
    log(f"  {msg} ({count}건)")
    if not ok:
        raise UploadError(msg)

    # 쇼핑몰 접속이 끼어 있어 결과 창까지 시간이 오래 걸린다 (건당 수 초).
    res = winui.wait_for_window(title_equals=APPLY_RESULT_WINDOW,
                                timeout=max(300.0, 20.0 * count))
    if res is None:
        raise UploadError("'송장번호수정 결과' 창이 뜨지 않았습니다.")
    time.sleep(2.0)

    status = ""
    for k in winui.children(res[0]):
        tx = winui.ctrl_text(k).strip()
        if tx.startswith("오류") and "발생건은" not in tx:
            status = tx
            break
    log(f"  반영 결과: {status or '(상태 문구 없음)'}")

    if close_result:
        _close_window(APPLY_RESULT_WINDOW, log)
    return status


def result_is_clean(status: str) -> bool:
    """'송장번호수정 결과' 문구가 '오류 없음'인가."""
    return status.startswith("오류없음")


def cleanup_stray_dialogs():
    """중단 후 남아 있을 수 있는 확인창/결과창을 정리한다.

    중간에 멈추면 '발송정보일괄등록' 창이나 결과 창이 떠 있는 채로 남는데,
    그대로 두면 다음 실행에서 Alt+U 가 새 창을 못 열거나 그리드가 가려져
    화면을 읽지 못한다. 여기서 닫히지 않아도 진행에는 지장이 없으므로
    조용히 시도만 한다.
    """
    for h, _t, _r in winui.find_windows(title_equals=CONFIRM_WINDOW):
        winui.click_dlg_button(h, winui.DLG_NO, label="확인 취소")
        time.sleep(0.6)

    def quiet(_msg):
        pass

    for title in (APPLY_RESULT_WINDOW, UPLOAD_RESULT_WINDOW, UPLOAD_WINDOW):
        _close_window(title, log=quiet)

