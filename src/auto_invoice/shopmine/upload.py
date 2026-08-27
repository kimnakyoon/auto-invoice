"""3~5단계 - 생성된 CSV를 샵마인에 올리고, 쇼핑몰까지 반영한다.

실제 화면 흐름 (2026-08-27 실측 검증):

    [배송중 탭]
      --[송장수정모드 켜기] 클릭--> [송장번호수정(S)] / [발송정보일괄등록(수정용)(U)] 등장
      --Alt+U--> '발송정보일괄등록' 창 (순수 WinForms, 컨트롤 전부 HWND 있음)
      --경로 EDIT 에 CSV 경로 설정 + [일괄등록(&S)]-->
          '발송정보일괄등록 결과' 창 ... 그리드의 '송장번호(수정용)' 컬럼이 채워짐
      --결과/등록창 닫기--> 대상 행 체크 --> [송장번호수정] 클릭
      --'선택한 N개의 주문을 [송장번호수정] 하시겠습니까?' --[예]-->
          '송장번호수정 결과' 창 ('오류없음.' 이면 성공)

안전장치의 핵심은 마지막 확인 대화상자다. 이 창이 '선택한 N개'라고 건수를
알려주므로, N이 기대와 다르면 [예] 대신 [아니요]를 눌러 아무것도 반영하지
않고 멈출 수 있다. 화면이 예상과 다를 때 그대로 진행하는 것이 가장 위험하다.

'발송정보일괄등록 결과' 창은 처리가 끝나도 라벨이 "처리중입니다."에서 안
바뀐다. 이 문구로 완료를 판단하면 안 된다 - 실제 반영 여부는 그리드의
'송장번호(수정용)' 컬럼으로 확인해야 한다.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import time
from pathlib import Path

from . import winui

VK_U = 0x55
VK_RETURN = 0x0D
VK_F8 = 0x77

UPLOAD_WINDOW = "발송정보일괄등록"
UPLOAD_RESULT_WINDOW = "발송정보일괄등록 결과"
APPLY_RESULT_WINDOW = "송장번호수정 결과"
CONFIRM_WINDOW = "질문"

# 창 상대좌표 (배송중 탭). 좌표에 의존하는 곳은 여기뿐이며,
# 클릭 전에 winui.safe_click 이 가림/커서도달을 검증한다.
REL_EDIT_MODE_TOGGLE = (77, 285)     # [송장수정모드 켜기/끄기]
REL_APPLY_BUTTON = (202, 285)        # [송장번호수정(S)]
REL_FIRST_ROW_CHECKBOX = (44, 415)   # 그리드 첫 데이터 행의 체크박스
SEARCH_EDIT_HINT = (98, 210)         # 수집 결과 필터 입력란
FILTER_STATUS_HINT = (495, 222)      # '필터안됨.' / '"xxx"(으)로 필터됨.' 라벨

# 행 체크박스 상태 판정. 실측된 색은 세 가지다:
#   (25,110,191) / (0,95,184)  체크됨      - 파랑
#   (243,243,243)              미체크      - 흰색
#   (130,135,144)              그리드 잠김 - 무채색 회색
# 밝기로 가르면 '잠김'을 체크로 오판한다. 파랑인지(B가 R보다 충분히 큰지)로
# 판정해야 세 상태가 정확히 갈린다.
CHECKBOX_BLUE_MARGIN = 40

# [송장번호수정]은 실제로 쇼핑몰에 접속해 송장을 등록한다. 그동안 '쇼핑몰 연결'
# 창이 뜨고 그리드가 잠기는데, 이를 기다리지 않고 다음 건을 클릭하면 클릭이
# 먹지 않는다. 8건 중 5건이 처음에 이 이유로 반영되지 않았다.
BUSY_WINDOW = "쇼핑몰 연결"


def _is_checked(color) -> bool:
    """체크박스가 체크됐는지 (파란색인지) 판정한다."""
    if color is None:
        return False
    r, g, b = color
    return b - r > CHECKBOX_BLUE_MARGIN


def _is_grid_locked(color) -> bool:
    """그리드가 잠긴(로딩 중) 상태의 무채색 회색인지."""
    if color is None:
        return True
    r, g, b = color
    return abs(b - r) <= CHECKBOX_BLUE_MARGIN and sum(color) / 3.0 < 200


def wait_until_idle(timeout: float = 180.0, log=print) -> bool:
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


class UploadError(RuntimeError):
    """업로드/반영 도중 안전하게 중단해야 하는 상황."""


def _main_window():
    wins = winui.find_windows(title_startswith="ShopMine::")
    if not wins:
        raise UploadError("샵마인이 실행 중이지 않습니다.")
    return wins[0][0]


def _children(parent):
    u = winui.u
    kids = []
    u.EnumChildWindows(parent, winui.ENUM(lambda h, l: (kids.append(h), True)[1]), 0)
    return kids


def _class_of(hwnd):
    b = ctypes.create_unicode_buffer(256)
    winui.u.GetClassNameW(hwnd, b, 256)
    c = b.value
    if c.startswith("WindowsForms10.") and "." in c:
        return c.split(".")[1]
    return c


def _find_control(parent, cls_name, predicate):
    for k in _children(parent):
        if _class_of(k) == cls_name and predicate(k):
            return k
    return None


def _rect(hwnd):
    r = wt.RECT()
    winui.u.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _click_control(hwnd, owner, label):
    """자식 컨트롤을 그 중심좌표로 클릭한다 (가림/커서도달 검증 포함).

    사용자가 실행 중에 마우스를 움직이면 커서가 목표에 도달하지 못하는데,
    winui.move_click 이 그때 RuntimeError 를 낸다. 파이프라인이 다루는
    UploadError 로 바꿔서 '안전하게 중단'으로 처리되게 한다.
    """
    r = _rect(hwnd)
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    winui.bring_to_front(owner)
    time.sleep(0.35)
    if not winui.is_descendant(winui.window_at(cx, cy), owner):
        raise UploadError(f"[{label}] 버튼이 다른 창에 가려져 있습니다.")
    try:
        winui.move_click(cx, cy)
    except RuntimeError as e:
        raise UploadError(
            f"[{label}] {e} (실행 중 마우스를 움직이면 이렇게 됩니다)") from e


def find_search_edit(main_hwnd):
    """'수집 결과 필터' 입력란을 창 상대 위치로 찾는다."""
    base = _rect(main_hwnd)

    def is_search(k):
        rr = _rect(k)
        rel = (rr.left - base.left, rr.top - base.top)
        return (abs(rel[0] - SEARCH_EDIT_HINT[0]) < 40
                and abs(rel[1] - SEARCH_EDIT_HINT[1]) < 40
                and winui.u.IsWindowVisible(k))

    return _find_control(main_hwnd, "EDIT", is_search)


def _close_window(title, log=print):
    for h, _t, _rect_ in winui.find_windows(title_equals=title):
        btn = _find_control(h, "BUTTON", lambda k: winui.ctrl_text(k).startswith("닫기"))
        if btn is None:
            continue
        try:
            _click_control(btn, h, f"{title} 닫기")
            log(f"  '{title}' 닫음")
        except UploadError as e:
            log(f"  경고: {e}")
        time.sleep(1.2)


def ensure_edit_mode(log=print) -> None:
    """송장수정모드를 켠다 (이미 켜져 있으면 그대로 둔다).

    Alt+U 로 업로드 창이 열리는지로 현재 모드를 판정한다 - 툴바 글자를
    읽을 수 없기 때문(버튼이 HWND를 갖지 않음)이다.
    """
    main = _main_window()
    if not winui.bring_to_front(main):
        raise UploadError("샵마인 창을 앞으로 가져오지 못했습니다.")
    time.sleep(0.5)

    winui.key(VK_U, alt=True)
    if winui.wait_for_window(title_equals=UPLOAD_WINDOW, timeout=3.0) is not None:
        log("  송장수정모드: 이미 켜져 있음 (업로드 창 열림)")
        return

    ok, msg = winui.safe_click(main, REL_EDIT_MODE_TOGGLE, label="송장수정모드 켜기")
    log(f"  {msg}")
    if not ok:
        raise UploadError(msg)
    time.sleep(1.5)


def bulk_register(csv_path, log=print) -> None:
    """3~4단계: CSV를 발송정보일괄등록(수정용)으로 올린다."""
    csv_file = Path(csv_path).resolve()
    if not csv_file.exists():
        raise UploadError(f"CSV 파일이 없습니다: {csv_file}")
    csv_str = str(csv_file).replace("/", "\\")

    main = _main_window()
    existing = winui.find_windows(title_equals=UPLOAD_WINDOW)
    if existing:
        upload_hwnd = existing[0][0]
    else:
        winui.bring_to_front(main)
        time.sleep(0.4)
        winui.key(VK_U, alt=True)
        found = winui.wait_for_window(title_equals=UPLOAD_WINDOW, timeout=15.0)
        if found is None:
            raise UploadError(
                "Alt+U 후 '발송정보일괄등록' 창이 열리지 않았습니다. "
                "송장수정모드가 꺼져 있거나 배송중 탭이 아닐 수 있습니다.")
        upload_hwnd = found[0]
    log(f"  발송정보일괄등록 창 확인 (hwnd={upload_hwnd})")

    def is_path_edit(k):
        if winui.ctrl_text(winui.u.GetParent(k)) != "엑셀 파일 열기":
            return False
        rr = _rect(k)
        return rr.right - rr.left > 200

    path_edit = _find_control(upload_hwnd, "EDIT", is_path_edit)
    if path_edit is None:
        raise UploadError("엑셀 경로 입력란을 찾지 못했습니다.")

    if not winui.set_ctrl_text(path_edit, csv_str):
        raise UploadError(f"경로가 입력란에 반영되지 않았습니다: {csv_str}")
    log(f"  경로 입력: {csv_str}")

    submit = _find_control(upload_hwnd, "BUTTON",
                           lambda k: winui.ctrl_text(k).startswith("일괄등록"))
    if submit is None:
        raise UploadError("[일괄등록] 버튼을 찾지 못했습니다.")

    _click_control(submit, upload_hwnd, "일괄등록")
    log("  [일괄등록] 클릭")

    if winui.wait_for_window(title_equals=UPLOAD_RESULT_WINDOW, timeout=30.0) is None:
        raise UploadError("'발송정보일괄등록 결과' 창이 뜨지 않았습니다.")
    time.sleep(2.0)
    log("  일괄등록 결과 창 확인")

    _close_window(UPLOAD_RESULT_WINDOW, log)
    _close_window(UPLOAD_WINDOW, log)


def filter_status(main_hwnd) -> str:
    """샵마인이 표시하는 현재 필터 상태 문구를 읽는다.

    '필터안됨.' 또는 '" 12345"(으)로 필터됨.' 형태다. 이 문구가 필터가
    실제로 걸렸는지를 확인할 수 있는 유일하게 확실한 근거다 - 그리드 셀은
    HWND가 없어 읽을 수 없기 때문이다.
    """
    base = _rect(main_hwnd)
    for k in _children(main_hwnd):
        if not winui.u.IsWindowVisible(k) or _class_of(k) != "STATIC":
            continue
        rr = _rect(k)
        rel = (rr.left - base.left, rr.top - base.top)
        if abs(rel[0] - FILTER_STATUS_HINT[0]) < 220 and abs(rel[1] - FILTER_STATUS_HINT[1]) < 30:
            tx = winui.ctrl_text(k)
            if "필터" in tx:
                return tx.strip()
    return ""


def filter_grid(keyword, log=print, verify: bool = True) -> None:
    """수집 결과 필터로 목록을 좁힌다. 빈 값이면 F8로 초기화한다.

    필터가 실제로 걸렸는지 반드시 확인한다. 필터가 안 걸린 채로 다음 단계에
    가면 목록 첫 행은 '엉뚱한 주문'이고, 그 상태로 체크 후 [송장번호수정]을
    누르면 확인 대화상자는 여전히 '선택한 1개'라고 말한다. 즉 건수 검증만으로는
    막을 수 없다.
    """
    main = _main_window()
    winui.bring_to_front(main)
    time.sleep(0.4)
    edit = find_search_edit(main)
    if edit is None:
        raise UploadError("검색 필터 입력란을 찾지 못했습니다.")
    winui.set_ctrl_text(edit, keyword or "")
    time.sleep(0.3)
    winui.key(VK_RETURN if keyword else VK_F8)
    time.sleep(2.5)

    if not keyword:
        log("  목록 필터 초기화 (F8)")
        return

    status = filter_status(main)
    if verify and keyword not in status:
        raise UploadError(
            f"필터가 '{keyword}'로 걸리지 않았습니다 (화면 표시: {status or '(읽을 수 없음)'}). "
            "목록에 없는 주문이거나 화면이 갱신되지 않았습니다. "
            "이대로 진행하면 엉뚱한 주문이 반영될 수 있어 중단합니다.")
    log(f"  목록 필터: {status}")


def ensure_row_checked(main_hwnd, log=print, attempts: int = 4) -> None:
    """그리드 첫 행의 체크박스가 '실제로 체크될 때까지' 확인하며 클릭한다.

    클릭이 들어갔다고 체크되는 게 아니다. 목록 필터를 바꾼 직후에는 그리드가
    아직 갱신 중이라 클릭이 먹지 않는데, 그 상태로 [송장번호수정]을 누르면
    샵마인이 '선택 0건'으로 보고 확인 대화상자를 아예 띄우지 않는다.
    실제로 8건 중 5건이 이 이유로 반영되지 않았다.
    """
    base = _rect(main_hwnd)
    ax = base.left + REL_FIRST_ROW_CHECKBOX[0]
    ay = base.top + REL_FIRST_ROW_CHECKBOX[1]

    for i in range(attempts):
        color = winui.pixel(ax, ay)
        if _is_checked(color):
            log(f"  행 선택 확인됨 (체크 색 {color})")
            time.sleep(0.6)     # 그리드가 선택을 반영할 틈을 준다
            return
        if _is_grid_locked(color):
            # 아직 로딩/연결 중이다. 클릭해봐야 먹지 않으므로 기다린다.
            wait_until_idle(log=log)
            time.sleep(1.5)
            continue
        if not winui.is_descendant(winui.window_at(ax, ay), main_hwnd):
            raise UploadError("행 체크박스가 다른 창에 가려져 있습니다.")
        winui.move_click(ax, ay)
        time.sleep(0.8 + 0.4 * i)      # 갱신이 늦을 수 있으니 점점 더 기다린다

    raise UploadError(
        f"행 체크박스가 체크되지 않았습니다 (마지막 색 {winui.pixel(ax, ay)}). "
        "목록이 비었거나 화면이 아직 갱신 중일 수 있습니다.")


def apply_tracking(expected_count: int, log=print, select_rows: bool = True,
                   close_result: bool = True) -> str:
    """5단계: 체크한 행을 [송장번호수정]으로 쇼핑몰까지 반영한다.

    확인 대화상자가 알려주는 건수가 expected_count 와 다르면 [아니요]를 눌러
    아무것도 반영하지 않고 UploadError 를 낸다. 이것이 이 파이프라인 전체의
    마지막이자 가장 중요한 안전장치다.
    """
    main = _main_window()
    winui.bring_to_front(main)
    time.sleep(0.5)

    if select_rows:
        ensure_row_checked(main, log=log)

    ok, msg = winui.safe_click(main, REL_APPLY_BUTTON, label="송장번호수정")
    log(f"  {msg}")
    if not ok:
        raise UploadError(msg)

    dlg = winui.wait_for_window(title_equals=CONFIRM_WINDOW, timeout=15.0)
    if dlg is None:
        raise UploadError("[송장번호수정] 확인 대화상자가 뜨지 않았습니다.")
    confirm_hwnd = dlg[0]

    message = ""
    for k in _children(confirm_hwnd):
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

    # 쇼핑몰 접속이 끼어 있어 결과 창까지 시간이 걸릴 수 있다.
    res = winui.wait_for_window(title_equals=APPLY_RESULT_WINDOW, timeout=180.0)
    if res is None:
        raise UploadError("'송장번호수정 결과' 창이 뜨지 않았습니다.")
    time.sleep(2.0)

    status = ""
    for k in _children(res[0]):
        tx = winui.ctrl_text(k).strip()
        if tx.startswith("오류") and "발생건은" not in tx:
            status = tx
            break
    log(f"  반영 결과: {status or '(상태 문구 없음)'}")

    if close_result:
        _close_window(APPLY_RESULT_WINDOW, log)
    return status


def apply_one_by_one(order_ids, log=print) -> list:
    """주문번호별로 목록을 1건씩 좁혀가며 [송장번호수정]을 반영한다.

    한 번에 여러 건을 체크하지 않는 이유: 확인 대화상자의 건수 검증이
    '항상 1개'라는 가장 단순하고 확실한 형태로 작동하기 때문이다. 필터가
    잘못 걸려 다른 주문이 딸려오면 즉시 건수가 어긋나 중단된다.

    반환: [(주문번호, 상태문자열 또는 예외메시지), ...]
    """
    results = []
    for i, order_id in enumerate(order_ids, 1):
        log(f"  ({i}/{len(order_ids)}) 주문 {order_id}")
        last_error = None
        # 확인 대화상자가 안 뜬 실패는 '아무것도 반영되지 않은' 상태이므로
        # 재시도해도 중복 반영 위험이 없다.
        for attempt in (1, 2):
            try:
                # 앞 건의 쇼핑몰 연결이 끝나기 전에 다음 건을 건드리면 안 된다.
                wait_until_idle(log=lambda m: log(f"    {m.strip()}"))
                filter_grid(order_id, log=lambda m: log(f"    {m.strip()}"))
                status = apply_tracking(1, log=lambda m: log(f"    {m.strip()}"))
                results.append((order_id, status))
                last_error = None
                break
            except UploadError as e:
                last_error = e
                log(f"    {'실패' if attempt == 2 else '1차 실패, 재시도'}: {e}")
                _cleanup_stray_dialogs()
        if last_error is not None:
            results.append((order_id, f"ERROR: {last_error}"))
    return results


def _cleanup_stray_dialogs():
    """중단 후 남아 있을 수 있는 확인창/결과창을 정리한다."""
    for h, _t, _r in winui.find_windows(title_equals=CONFIRM_WINDOW):
        winui.click_dlg_button(h, winui.DLG_NO, label="확인 취소")
        time.sleep(0.6)
    _close_window(APPLY_RESULT_WINDOW, log=lambda m: None)
