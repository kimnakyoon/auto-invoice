"""1단계 - 샵마인에서 주문 목록 엑셀을 내보낸다.

실제 화면 흐름 (2026-08-27 실측):

    [배송중 탭] --Alt+X--> '엑셀 파일 생성' 창 (WebView2)
        --[엑셀 파일 생성] 클릭--> '다른 이름으로 저장' (#32770)
        --경로 입력 + Enter--> '엑셀파일을 여시겠습니까?' (#32770)
        --[아니요]--> 끝

'엑셀 파일 생성' 창만 WebView2(Chrome_WidgetWin_1)라 버튼이 HWND를 갖지
않는다. 그래서 그 버튼 하나만 색으로 찾아 클릭하고, 나머지 표준 대화상자는
전부 컨트롤 ID로 다룬다.

양식은 샵마인 쪽에 저장된 '송장 자동화' 프리셋을 그대로 쓴다. 그 양식이
수령인 / 마켓 주문번호 / 상품URL / 주문옵션 4개 컬럼을 내보내며, 이는
excel_io.read_pending_orders()가 요구하는 것과 정확히 일치한다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import winui

# [엑셀 파일 생성] 버튼의 보라파랑 (#5b63d3 계열)
EXPORT_BUTTON_COLOR = (91, 99, 211)

VK_X = 0x58
VK_RETURN = 0x0D
VK_A = 0x41

SAVE_DIALOG_TITLE = "다른 이름으로 저장"
OPEN_ASK_TITLE = "질문"


class ExportError(RuntimeError):
    """내보내기 도중 안전하게 중단해야 하는 상황."""


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

    # 3) [엑셀 파일 생성] 버튼을 색으로 찾아 클릭
    winui.bring_to_front(export_hwnd)
    time.sleep(0.6)
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

    # 4) 저장 대화상자 -> 경로 입력
    dlg = winui.wait_for_window(title_equals=SAVE_DIALOG_TITLE, timeout=timeout)
    if dlg is None:
        raise ExportError("'다른 이름으로 저장' 창이 뜨지 않았습니다.")
    save_hwnd = dlg[0]
    if not winui.bring_to_front(save_hwnd):
        raise ExportError("저장 대화상자를 앞으로 가져오지 못했습니다.")
    time.sleep(0.4)

    winui.ctrl_key(VK_A)            # 기본 파일명 전체 선택
    time.sleep(0.2)
    winui.type_text(str(target).replace("/", "\\"))
    time.sleep(0.4)
    winui.key(VK_RETURN)
    log(f"  저장 경로 입력: {target}")

    # 5) '엑셀파일을 여시겠습니까?' -> 아니요
    ask = winui.wait_for_window(title_equals=OPEN_ASK_TITLE, timeout=15.0)
    if ask is not None:
        ok, msg = winui.click_dlg_button(ask[0], winui.DLG_NO, label="엑셀 열기 묻기")
        log(f"  {msg}")

    # 6) 결과 확인
    end = time.time() + timeout
    while time.time() < end:
        if target.exists() and target.stat().st_size > 0:
            break
        time.sleep(0.5)
    else:
        raise ExportError(f"엑셀 파일이 생성되지 않았습니다: {target}")

    if winui.find_windows(title_equals=SAVE_DIALOG_TITLE):
        raise ExportError("저장 대화상자가 아직 열려 있습니다 - 경로가 거부됐을 수 있습니다.")

    try:
        os.remove(tmp_shot)
    except OSError:
        pass

    log(f"  엑셀 생성 완료: {target} ({target.stat().st_size:,} bytes)")
    return target
