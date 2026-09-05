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
      --'송장번호(수정용)' 컬럼 헤더 두 번 클릭--> 채워진 행이 위로 정렬됨
      --위에서부터 채워진 행만 체크 --> [송장번호수정] 클릭
      --'선택한 N개의 주문을 [송장번호수정] 하시겠습니까?' --[예]-->
          '송장번호수정 결과' 창: 아래쪽 '총 N개의 주문중 M건의 주문이 처리
          되었습니다. 오류건은 K건입니다.' 요약이 뜰 때까지 = 처리가 끝날
          때까지 기다렸다가 오류를 읽고 [확인]을 눌러 닫는다
          ('오류없음.' + 오류건 0건이면 성공)
      --오류가 없으면 [송장수정모드 끄기]

안전장치의 핵심은 마지막 확인 대화상자다. 이 창이 '선택한 N개'라고 건수를
알려주므로, N이 기대와 다르면 [예] 대신 [아니요]를 눌러 아무것도 반영하지
않고 멈출 수 있다. 화면이 예상과 다를 때 그대로 진행하는 것이 가장 위험하다.

'발송정보일괄등록 결과' 창은 처리가 끝나도 라벨이 "처리중입니다."에서 안
바뀐다. 이 문구로 완료를 판단하면 안 된다 - 실제 반영 여부는 그리드의
'송장번호(수정용)' 컬럼으로 확인해야 한다(grid.py).
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

from . import filedialog, grid, winui

VK_U = 0x55
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
        time.sleep(1.0)
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
    time.sleep(0.15)
    winui.press_button(hwnd)


def _toolbar_click(main_hwnd, rel, label, until, *, attempts=3, timeout=8.0,
                   log=print):
    """툴바 버튼을 누르고 **실제로 반응했는지**까지 확인한다. 반응이 없으면 다시 누른다.

    툴바(ToolStrip) 버튼은 좌표 클릭이 조용히 무시될 때가 있다. 창이 활성
    상태가 아니면 WinForms 가 첫 클릭을 '창 활성화'로 삼켜버리고, 마우스가
    들어온 것을 아직 인식하지 못했어도 그냥 버린다. 실패 신호가 전혀 없어서
    '눌렀는가'로는 판정할 수 없고 '반응했는가'로 봐야 한다.

    2026-08-29 에 [송장번호수정]과 [송장수정모드 끄기]가 연달아 이렇게 무시돼
    파이프라인이 멈췄다. 그때 그리드 읽기와 [전체선택](HWND 메시지)은 멀쩡히
    동작했다 - 좌표 클릭만 골라서 먹지 않았다. 그래서 좌표를 쓰는 툴바 버튼은
    전부 이 헬퍼를 거친다.

    until: 반응을 확인하는 함수. 반응했으면 참인 값(창 정보 등)을 돌려준다.
    반환: (until 이 돌려준 값 또는 None, 사람에게 보여줄 메시지)
    """
    last = ""
    for attempt in range(1, attempts + 1):
        if not winui.bring_to_front(main_hwnd):
            last = (f"[{label}] 샵마인 창을 앞으로 가져오지 못했습니다 - "
                    "다른 창(브라우저 등)이 앞을 막고 있습니다.")
        else:
            time.sleep(0.2)
            ok, msg = winui.safe_click(main_hwnd, rel, label=label)
            if ok:
                end = time.time() + timeout
                while time.time() < end:
                    got = until()
                    if got:
                        return got, msg
                    time.sleep(0.15)
                last = (f"{msg} - 그런데 {timeout:.0f}초 동안 아무 반응이 없습니다 "
                        "(툴바가 클릭을 받지 않았습니다)")
            else:
                last = msg
        if attempt < attempts:
            log(f"  {last} - 다시 누릅니다 ({attempt}/{attempts})")
            time.sleep(0.7)
    return None, last


# 창을 닫는 버튼 이름. 창마다 [닫기] 또는 [확인]으로 다르고, 어느 쪽이든
# 누르지 않으면 창이 남아 다음 조작을 가린다. 앞에 있는 것부터 찾는다.
CLOSE_CAPTIONS = ("닫기", "확인")
# '송장번호수정 결과' 창만 [확인]을 먼저 찾는다 - 이 창의 버튼이 [확인]인데
# [닫기]만 찾던 예전 코드가 아무것도 못 눌러서, 결과를 확인하지도 닫지도 못한
# 채 다음 단계로 넘어갔다.
APPLY_RESULT_CAPTIONS = ("확인", "닫기")

# 요약 문구를 끝내 못 읽었을 때만 쓰는 보조 기준. 상태 라벨('오류없음.' 등)이
# 이미 채워져 있고 창 글자가 이만큼(초) 안 바뀌면 끝난 것으로 본다.
RESULT_STABLE_SECONDS = 15.0

# '송장번호수정 결과' 창 맨 아래 요약 문구:
#   "총 2개의 주문중 2건의 주문이 처리 되었습니다. 오류건은 0건입니다."
# 이 문구는 샵마인이 쇼핑몰 접속을 **다 끝낸 뒤에야** 채워진다. 창이 뜬 것만
# 으로는 아직 처리 중이다. 그래서 '다음 동작으로 넘어가도 되는가'와 '오류가
# 있었는가'를 함께 알려주는, 이 창에서 가장 믿을 만한 신호다.
RE_SUMMARY_TOTAL = re.compile(r"총\s*([\d,]+)\s*개")
RE_SUMMARY_DONE = re.compile(r"([\d,]+)\s*건의\s*주문이\s*처리")
RE_SUMMARY_ERRORS = re.compile(r"오류건은\s*([\d,]+)\s*건")

# 결과 창에 늘 붙어 있는 안내/버튼 글자. 오류가 아니므로 결과 엑셀로 넘기지
# 않는다 - 특히 '오류 발생건은 반드시 확인 바랍니다'는 오류가 0건이어도 늘
# 빨간 글씨로 떠 있어서, 그대로 넘기면 없는 오류를 있는 것처럼 보이게 한다.
RESULT_BOILERPLATE = ("오류 발생건은", "선택한 주문을", "엑셀파일생성", "도움말", "총 ")


def _buttons(hwnd):
    """창에 보이는 버튼들 - (핸들, 글자) 목록."""
    return [(k, winui.ctrl_text(k)) for k in winui.children(hwnd)
            if winui.class_of(k) == "BUTTON" and winui.u.IsWindowVisible(k)]


def _close_window(title, log=print, captions=CLOSE_CAPTIONS):
    for h, _t, _r in winui.find_windows(title_equals=title):
        buttons = _buttons(h)
        btn = caption = None
        for want in captions:
            btn = next((k for k, tx in buttons if tx.startswith(want)), None)
            if btn is not None:
                caption = want
                break
        if btn is None and len(buttons) == 1:
            # 이름이 예상과 다른 경우. 결과 창에 버튼이 하나뿐이면 그게 닫는
            # 버튼이다 - 창을 못 닫으면 다음 조작이 전부 가려지므로, 이름을
            # 남기고 그 하나를 누른다 (다음에 이름을 CLOSE_CAPTIONS에 넣으면 된다).
            btn, caption = buttons[0][0], buttons[0][1]
            log(f"  '{title}' 창의 버튼 이름이 {caption!r} 입니다 - 이걸 누릅니다.")
        if btn is None:
            names = ", ".join(repr(tx) for _k, tx in buttons) or "(버튼 없음)"
            log(f"  경고: '{title}' 창에서 [{'/'.join(captions)}] 버튼을 찾지 못했습니다 "
                f"(그 창의 버튼: {names}).")
            continue
        _click_control(btn, h, f"{title} {caption}")
        if winui.wait_for_window_gone(title_equals=title, timeout=8.0):
            log(f"  '{title}' [{caption}] 눌러 닫음")
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


def _toggle_edit_mode(main_hwnd, label, want_on: bool, log=print):
    """[송장수정모드 켜기/끄기]를 누르고 모드가 실제로 바뀔 때까지 확인한다.

    예전에는 누른 뒤 2초를 세고 한 번만 확인했다. 실측하면 0.9초면 바뀌지만,
    클릭이 무시되면 2초를 기다려봐야 소용이 없다. 고정 대기 대신 폴링하고,
    반응이 없으면 다시 누른다(_toolbar_click).
    """
    return _toolbar_click(main_hwnd, REL_EDIT_MODE_TOGGLE, label,
                          lambda: edit_mode_on(main_hwnd) == want_on, log=log)


def ensure_edit_mode(log=print) -> None:
    """송장수정모드를 켠다 (이미 켜져 있으면 그대로 둔다)."""
    main = main_window()
    if edit_mode_on(main):
        log("  송장수정모드: 이미 켜져 있음")
        return
    ok, msg = _toggle_edit_mode(main, "송장수정모드 켜기", True, log=log)
    if not ok:
        raise UploadError(
            f"[송장수정모드 켜기]를 눌렀는데 모드가 켜지지 않았습니다 ({msg}). "
            "배송중 탭이 아니거나 화면 상태가 다를 수 있습니다.")
    log(f"  {msg}")
    log("  송장수정모드 켜짐 확인")


def disable_edit_mode(log=print) -> None:
    """송장수정모드를 끈다 (모두 정상 반영된 뒤 마무리)."""
    main = main_window()
    if not edit_mode_on(main):
        log("  송장수정모드: 이미 꺼져 있음")
        return
    ok, msg = _toggle_edit_mode(main, "송장수정모드 끄기", False, log=log)
    if not ok:
        raise UploadError(f"[송장수정모드 끄기]를 눌렀는데 모드가 꺼지지 않았습니다 ({msg}).")
    log(f"  {msg}")
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


# 필터를 건 뒤 상태 라벨이 바뀌기를 기다리는 최대 시간. 예전에는 여기에
# 고정 2.5초가 박혀 있었는데, 주문번호로 한 건씩 필터를 거는 7단계에서는 그게
# 건당 2.5초씩 곱해졌다. 라벨은 보통 0.3초 안에 바뀌고, 그리드가 다 그려졌는지는
# 뒤따르는 grid.wait_ready 가 화면을 보고 따로 확인한다 - 여기서 또 기다릴
# 이유가 없다.
FILTER_WAIT_SECONDS = 12.0


def _wait_filter_status(main_hwnd, keyword: str,
                        timeout: float = FILTER_WAIT_SECONDS) -> str:
    """필터 상태 라벨에 keyword 가 나타날 때까지 기다렸다가 그 문구를 돌려준다."""
    end = time.time() + timeout
    status = ""
    while True:
        status = filter_status(main_hwnd)
        if keyword in status or time.time() >= end:
            return status
        time.sleep(0.1)


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
    time.sleep(0.2)
    edit = _search_edit(main)
    if edit is None:
        raise UploadError("수집 결과 필터 입력란을 찾지 못했습니다.")
    button = _search_button(main)
    if button is None:
        raise UploadError("[수집결과내 검색] 버튼을 찾지 못했습니다.")

    before = filter_status(main)
    if not winui.set_ctrl_text(edit, keyword):
        raise UploadError(f"필터 입력란에 '{keyword}'가 들어가지 않았습니다.")
    time.sleep(0.2)
    _click_control(button, main, "수집결과내 검색")

    if keyword in before:
        # 같은 키워드로 다시 거는 경우엔 라벨이 바뀌지 않아 기다릴 근거가 없다.
        time.sleep(1.0)
        status = filter_status(main)
    else:
        status = _wait_filter_status(main, keyword)
    if keyword not in status:
        raise UploadError(
            f"필터가 '{keyword}'로 걸리지 않았습니다 (화면 표시: {status or '(읽을 수 없음)'}). "
            "이대로 진행하면 엉뚱한 주문이 선택될 수 있어 중단합니다.")
    log(f"  목록 필터: {status}")


def reset_filter(log=print) -> None:
    """필터초기화(F8). 체크해둔 행은 그대로 남는다."""
    main = main_window()
    winui.bring_to_front(main)
    time.sleep(0.2)
    edit = _search_edit(main)
    if edit is not None:
        winui.set_ctrl_text(edit, "")
        time.sleep(0.15)
    winui.bring_to_front(main)
    time.sleep(0.15)
    winui.key(VK_F8)
    # F8 뒤에는 상태 라벨이 갱신되지 않아(filter_status 주석) 라벨로는 확인할 수
    # 없다. 대신 목록이 다시 그려져 안정될 때까지 화면을 본다 - 고정 2.5초보다
    # 정확하면서 대개 더 빠르다. 여기서 확인하지 못해도 다음 단계가 각자
    # wait_ready 로 다시 확인하므로 진행은 막지 않는다.
    with contextlib.suppress(grid.GridError):
        grid.wait_ready(main, timeout=8.0, log=_quiet)
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
            time.sleep(1.0)
            continue
        time.sleep(0.5)
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


def _wait_path_shown(upload_hwnd, csv_str, timeout: float = 3.0) -> str:
    """대화상자가 닫힌 뒤 경로칸이 채워지기를 잠깐 기다렸다가 그 글자를 돌려준다.

    샵마인은 대화상자가 닫힌 다음 자기 UI 스레드에서 경로칸을 채운다 -
    닫히자마자 읽으면 아직 빈 칸일 수 있다. 경로칸을 못 찾으면 빈 문자열.
    """
    end = time.time() + timeout
    edit = None
    while True:
        edit = edit or _path_edit(upload_hwnd)
        shown = winui.ctrl_text(edit) if edit is not None else ""
        if csv_str in shown or time.time() >= end:
            return shown
        time.sleep(0.2)


def _pick_csv_file(upload_hwnd, csv_str, log=print) -> None:
    """[찾아보기]로 CSV 파일을 고른다.

    경로 입력란에 WM_SETTEXT 로 값만 넣으면 **화면에는 제대로 보이는데
    [일괄등록]은 0건을 처리한다.** 샵마인이 그 텍스트가 아니라 '대화상자로
    고른 파일'을 쓰기 때문이다. 결과 창도 빈 채로 "준비."만 뜨고 오류조차
    알려주지 않아서, 3건이 조용히 반영되지 않은 걸 그리드를 보고서야 알았다.
    그래서 반드시 대화상자를 거친다.

    대화상자 안에서 경로를 넣고 [열기]를 누르는 규칙은 filedialog.py 에 있다
    (친 값 되읽기, 포커스와 무관한 버튼 클릭). 여기서는 닫힌 뒤 샵마인 경로칸이
    실제로 채워졌는지 보고, 비어 있으면 처음부터 한 번 더 한다 - 사람이 그 순간
    대화상자를 닫아버리면 그렇게 된다 (2026-09-05).
    """
    browse = winui.find_child(upload_hwnd, "BUTTON",
                              lambda k: winui.ctrl_text(k).startswith("찾아보기"))
    if browse is None:
        raise UploadError("[찾아보기] 버튼을 찾지 못했습니다.")

    error = ""
    for attempt in (1, 2):
        winui.bring_to_front(upload_hwnd)
        time.sleep(0.2)
        winui.press_button(browse)

        dlg = winui.wait_for_window(title_equals=OPEN_DIALOG_TITLE, timeout=15.0)
        if dlg is None:
            raise UploadError(f"[찾아보기] 후 '{OPEN_DIALOG_TITLE}' 대화상자가 뜨지 않았습니다.")
        try:
            entered = filedialog.fill_filename(dlg[0], csv_str, log=log)
            if entered != csv_str:
                raise filedialog.DialogError(
                    f"파일 이름 칸에 경로가 들어가지 않았습니다 (들어간 값: {entered!r})")
            filedialog.commit(dlg[0], csv_str, log=log)
        except filedialog.DialogError as e:
            raise UploadError(
                f"{e} - 실행 중에 다른 창을 누르거나 키보드를 쓰면 이렇게 됩니다. "
                "손을 떼고 다시 실행해주세요.") from e

        if not winui.wait_for_window_gone(title_equals=OPEN_DIALOG_TITLE, timeout=15.0):
            raise UploadError(
                f"파일 선택 대화상자가 닫히지 않았습니다 - 경로가 거부됐을 수 있습니다: {csv_str}")

        shown = _wait_path_shown(upload_hwnd, csv_str)
        if csv_str in shown:
            log(f"  파일 선택: {csv_str}")
            return
        error = f"고른 파일이 경로칸에 반영되지 않았습니다 (화면: {shown!r})"
        if attempt == 1:
            log(f"  {error} - 파일 선택을 한 번 더 시도합니다 (2/2)")

    raise UploadError(
        f"{error}. 실행 중에 대화상자를 직접 닫거나 다른 창을 누르면 이렇게 "
        "됩니다 - 손을 떼고 [멈춘 지점부터 다시 시작]을 눌러주세요.")


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

def _quiet(_msg=""):
    pass


def select_registered_rows(order_ids=None, expected=None, log=print) -> int:
    """일괄등록으로 송장번호(수정용)가 채워진 행만 체크한다.

    송장수정모드에서 이 컬럼은 원래 값으로 미리 채워져 있고 경동택배·직접전달
    행만 비어 있다. 그 두 필터 안에서 '채워짐'은 곧 '이번 일괄등록이 반영됨'을
    뜻한다 - 조회에 실패해 CSV에 없던 건은 여전히 비어 있다.

    다만 **사람이 직접 채워 넣은 행**은 이 규칙에서 벗어난다. 미지원 사이트
    주문처럼 도구가 넘긴 건을 사람이 직접 조회해 이 칸에 적어두면, 화면만
    봐서는 우리가 올린 값과 구분이 안 된다 (2026-08-31 실제로 이 때문에
    '채워진 행 2건 / 예상 1건'으로 멈췄다. 수집결과내 검색은 원본 데이터만
    찾아서 우리가 올린 송장번호로는 그 행을 집어낼 수도 없다).

    그래서 두 단계로 고른다.

      1) 빠른 길 - 경동택배/직접 필터에서 헤더 정렬 후 채워진 행을 체크한다.
         건수가 예상과 맞으면 그대로 끝낸다 (평소엔 여기서 끝난다).
      2) 건수가 어긋나면 - 우리가 올린 **주문번호로 한 건씩 필터를 걸어**
         그 주문의 채워진 행만 다시 고른다. 마켓 주문번호는 원본 데이터라
         필터에 걸리므로, 사람이 채워둔 행은 애초에 화면에 들어오지 않는다.
         건당 3~4초 더 걸리는 대신 남의 행을 건드리지 않는다.

    order_ids/expected 를 주지 않으면 1)만 하고 그 결과를 돌려준다.
    """
    main = main_window()
    if not edit_mode_on(main):
        raise UploadError(
            "송장수정모드가 꺼져 있습니다. 그 상태에서는 '송장번호(수정용)' 컬럼이 "
            "화면에 없어 어떤 행이 반영됐는지 알 수 없습니다.")

    total = _select_by_courier(main, log=log)
    if expected is not None and total != expected and order_ids:
        log(f"  채워진 행이 {total}건으로 예상({expected}건)과 다릅니다 - "
            "사람이 직접 채워둔 행이 섞였을 수 있어, 우리가 올린 주문번호로 다시 고릅니다.")
        total = _select_by_order_id(main, order_ids, log=log)

    if total == 0:
        raise UploadError(
            "송장번호(수정용)가 채워진 행을 하나도 찾지 못했습니다. "
            "일괄등록이 실제로 반영되지 않았을 수 있어 중단합니다.")
    return total


def _select_by_courier(main_hwnd, log=print) -> int:
    """빠른 길: 경동택배/직접 필터에서 정렬 후 채워진 행을 전부 체크한다."""
    grid.clear_all_checks(main_hwnd, log=log)
    total = 0
    for keyword in TARGET_COURIERS:
        set_filter(keyword, log=log)
        # 필터를 건 다음 '송장번호(수정용)' 헤더를 두 번 눌러 채워진 행을 위로
        # 모은다. 정렬이 확인되면 빈 행이 나오는 순간 멈춘다 (grid.py 참고).
        sorted_ok = grid.sort_filled_first(main_hwnd, log=log)
        n = grid.check_filled_rows(main_hwnd, log=log, stop_at_empty=sorted_ok)
        log(f"  '{keyword}' 에서 송장이 채워진 {n}건 체크")
        total += n
    reset_filter(log=log)
    return total


def _select_by_order_id(main_hwnd, order_ids, log=print) -> int:
    """정확한 길: 주문번호로 한 건씩 필터를 걸어 그 주문의 채워진 행만 체크한다.

    앞 단계에서 걸어둔 체크를 먼저 전부 푼다 - 거기엔 남의 행이 섞여 있을 수
    있어서, 더하는 게 아니라 처음부터 다시 고르는 것이다.
    """
    grid.clear_all_checks(main_hwnd, log=log)
    unique = list(dict.fromkeys(order_ids))
    log(f"  주문번호 {len(unique)}건을 하나씩 확인합니다 (건당 3~4초)")
    total = 0
    missing = []
    for i, order_id in enumerate(unique, start=1):
        set_filter(order_id, log=_quiet)
        n = grid.check_filled_rows(main_hwnd, log=_quiet)
        total += n
        if n == 0:
            missing.append(order_id)
        log(f"    ({i}/{len(unique)}) {order_id}: {n}건 체크 (누적 {total}건)")
    reset_filter(log=log)
    if missing:
        log(f"  경고: 송장번호(수정용)가 비어 있어 넘긴 주문 {len(missing)}건 - "
            f"{', '.join(missing[:5])}{' 외' if len(missing) > 5 else ''}")
    return total


def _confirm_dialog():
    """지금 떠 있는 [송장번호수정] 확인 대화상자 (없으면 None)."""
    found = winui.find_windows(title_equals=CONFIRM_WINDOW)
    return found[0] if found else None


def _result_texts(hwnd) -> list[str]:
    """결과 창에 지금 보이는 글자들 (중복 없이, 나온 순서대로)."""
    out: list[str] = []
    for k in winui.children(hwnd):
        tx = winui.ctrl_text(k).strip()
        if tx and tx not in out:
            out.append(tx)
    return out


def _status_label(texts: list[str]) -> str:
    """'오류없음.' / '오류발생 N건' 같은 상태 라벨 (없으면 빈 문자열).

    빨간 안내 문구('오류 발생건은 반드시 확인 바랍니다...')와 아래쪽 요약
    문구('... 오류건은 0건입니다.')는 오류가 없어도 늘 떠 있으므로 뺀다.
    """
    return next((t for t in texts
                 if t.startswith("오류") and "발생건은" not in t
                 and "오류건은" not in t), "")


def _parse_summary(texts: list[str]) -> tuple[str, int | None, int | None, int | None]:
    """아래쪽 요약 문구를 (원문, 총건수, 처리건수, 오류건수)로 읽는다.

    요약 줄이 아직 없으면 ("", None, None, None) - 즉 아직 처리 중이다.
    문구는 떴는데 숫자만 못 읽는 경우가 있어 값마다 따로 None 을 돌려주고,
    읽어낸 것만 판정에 쓴다.
    """
    for t in texts:
        if not (RE_SUMMARY_ERRORS.search(t) or "주문이 처리" in t):
            continue

        def num(rx):
            m = rx.search(t)
            return int(m.group(1).replace(",", "")) if m else None

        return t, num(RE_SUMMARY_TOTAL), num(RE_SUMMARY_DONE), num(RE_SUMMARY_ERRORS)
    return "", None, None, None


def _wait_for_apply_result(hwnd, timeout: float, log=print) -> tuple[list[str], bool]:
    """결과 창의 처리가 **끝날 때까지** 기다린다. 반환: (창 글자들, 끝났는가)

    창은 쇼핑몰에 접속하는 동안 먼저 비어 있는 채로 떠 있다. 끝났다는 신호는
    아래쪽 요약 문구('총 N개의 주문중 M건의 주문이 처리 되었습니다')다. 이게
    뜨기 전에 다음 동작으로 넘어가면, 아직 반영되지도 않은 화면을 보고
    성공/실패를 판정하게 된다.

    처리 중에는 결과가 그리드로만 그려져 라벨 글자가 한참 그대로일 수 있다.
    그래서 '글자가 안 바뀐다'만으로는 끝났다고 보지 않는다 - 상태 라벨까지
    채워진 뒤에야 그 기준(RESULT_STABLE_SECONDS)을 보조로 쓴다.
    """
    started = time.time()
    end = started + timeout
    texts: list[str] = []
    previous: list[str] | None = None
    stable_since = started
    noted = 0.0
    while time.time() < end:
        texts = _result_texts(hwnd)
        line = _parse_summary(texts)[0]
        if line:
            # 요약이 채워졌다. 상태 라벨은 한 박자 늦게 붙을 때가 있어 잠깐만 더 본다.
            for _ in range(8):
                if _status_label(texts):
                    break
                time.sleep(0.25)
                texts = _result_texts(hwnd)
            log(f"  결과 창 처리 완료 ({time.time() - started:.0f}초): {line}")
            return texts, True
        if texts != previous:
            previous, stable_since = texts, time.time()
        elif (_status_label(texts)
              and time.time() - stable_since >= RESULT_STABLE_SECONDS):
            log("  요약 문구는 없지만 상태 문구가 있고 창 글자가 "
                f"{RESULT_STABLE_SECONDS:.0f}초째 그대로입니다 - "
                f"여기서 끝난 것으로 봅니다: {texts}")
            return texts, True
        if time.time() - started - noted >= 30.0:
            noted = time.time() - started
            log(f"  결과 창 처리 대기 중... ({noted:.0f}초)")
        time.sleep(0.5)
    return texts, False


def _read_apply_result(hwnd, timeout: float, log=print,
                       expected: int | None = None) -> tuple[str, list[str]]:
    """처리가 끝나길 기다린 뒤, 오류가 있었는지 판정한다.

    반환: (상태 문구, 오류 상세 줄들). 상태 문구가 '오류없음'으로 시작할
    때만(result_is_clean) 파이프라인이 다음 단계로 넘어간다. 그래서 조금이라도
    어긋나면 - 처리가 안 끝났거나, 오류건이 있거나, 처리 건수가 예상과
    다르거나, 문구를 아예 못 읽었거나 - 상태 문구를 '오류발생'으로 만들어
    거기서 멈추고 사람에게 넘어가게 한다.

    상세 줄은 창에 적힌 나머지 글자들이다. 어느 주문에서 났는지까지 알려주는
    경우가 있어서, 해석하지 않고 그대로 넘겨 결과 엑셀에 남긴다.
    """
    texts, finished = _wait_for_apply_result(hwnd, timeout, log=log)
    line, total, done, errors = _parse_summary(texts)
    status = _status_label(texts)

    problems: list[str] = []
    if not finished:
        problems.append(f"{timeout:.0f}초를 기다렸는데도 결과 창의 처리가 끝나지 "
                        "않았습니다 (요약 문구가 나오지 않음).")
    if errors:
        problems.append(f"쇼핑몰 반영 오류 {errors}건 - {line}")
    if total is not None and done is not None and done != total:
        problems.append(f"{total}건 중 {done}건만 처리됐습니다 - {line}")
    if expected is not None and done is not None and done != expected:
        problems.append(f"처리 건수가 예상과 다릅니다 (화면 {done}건 / 예상 {expected}건).")
    if status and not result_is_clean(status):
        problems.append(status)
    if not status and not line:
        problems.append("결과 문구('오류없음.' 등)를 읽지 못했습니다 - "
                        "샵마인 화면에서 직접 확인해주세요.")
    elif not status and not problems:
        # 라벨은 못 읽었지만 요약이 건수를 다 말해주고 어긋나는 게 없다.
        log(f"  상태 문구를 읽지 못해 요약으로 판정합니다: {line}")
        status = "오류없음."

    if not problems:
        # 오류가 없으면 창에 남은 다른 글자(제목/안내 문구)는 오류가 아니다.
        # 그걸 오류로 넘기면 결과 엑셀에 있지도 않은 오류 구역이 생긴다.
        return status, []

    details = [t for t in texts
               if t != status and t not in CLOSE_CAPTIONS
               and t not in (APPLY_RESULT_WINDOW, UPLOAD_RESULT_WINDOW)
               and not any(t.startswith(prefix) for prefix in RESULT_BOILERPLATE)
               and len(t) >= 4]
    if not status or result_is_clean(status):
        # 라벨은 '오류없음.'인데 건수가 어긋나는 경우까지 성공으로 흘려보내지 않는다.
        status = f"오류발생. {problems[0]}"
    out: list[str] = []
    for t in problems + details:
        if t not in out:
            out.append(t)
    return status, out


def apply_tracking(expected_count: int, log=print, close_result: bool = True,
                   result_timeout: float | None = None) -> tuple[str, list[str]]:
    """체크해둔 행을 [송장번호수정]으로 쇼핑몰까지 반영한다.

    확인 대화상자가 알려주는 건수가 expected_count 와 다르면 [아니요]를 눌러
    아무것도 반영하지 않고 UploadError 를 낸다. 이것이 이 파이프라인 전체의
    마지막이자 가장 중요한 안전장치다.

    [예]를 누른 뒤에는 '송장번호수정 결과' 창의 처리가 **끝날 때까지**
    기다린다 (아래쪽 '총 N개의 주문중 M건의 주문이 처리 되었습니다. 오류건은
    K건입니다.' 요약이 뜰 때까지). 그 다음에야 오류를 읽고 [확인]으로 창을
    닫는다. 어긋나는 게 하나라도 있으면 상태 문구가 '오류없음'이 아니게 되어
    다음 단계(체크 해제 / 송장수정모드 끄기)가 멈추고 화면이 그대로 남는다.
    반환: (상태 문구, 오류 상세 줄들).
    """
    main = main_window()

    # 확인 대화상자가 뜨는 것이 곧 '클릭이 먹었다'는 신호다. 안 뜨면 다시 누른다.
    # 이미 떠 있으면 그 창이 모달이라 다음 클릭은 어차피 무시되므로, 눌러서
    # 두 번 반영될 걱정은 없다.
    dlg, msg = _toolbar_click(main, REL_APPLY_BUTTON, "송장번호수정",
                              _confirm_dialog, log=log)
    log(f"  {msg}")
    if dlg is None:
        raise UploadError(
            "[송장번호수정] 확인 대화상자가 뜨지 않았습니다. "
            f"선택된 행이 없거나 화면이 갱신 중일 수 있습니다 ({msg}).")
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

    # 쇼핑몰 접속이 건당 수 초씩 걸리므로 기다리는 시간도 건수에 맞춘다.
    timeout = result_timeout if result_timeout is not None else max(180.0, 20.0 * count)
    status, details = _read_apply_result(res[0], timeout, log=log, expected=count)
    log(f"  반영 결과: {status or '(상태 문구 없음)'}")
    for line in details:
        log(f"    {line}")

    # 오류가 있든 없든 [확인]을 눌러 창을 닫는다. 이 창이 떠 있으면 다음
    # 조작(체크 해제 / 송장수정모드 끄기)이 전부 가려져 조용히 무시된다.
    if close_result:
        _close_window(APPLY_RESULT_WINDOW, log, captions=APPLY_RESULT_CAPTIONS)
    return status, details


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

    _close_window(APPLY_RESULT_WINDOW, log=quiet, captions=APPLY_RESULT_CAPTIONS)
    for title in (UPLOAD_RESULT_WINDOW, UPLOAD_WINDOW):
        _close_window(title, log=quiet)

