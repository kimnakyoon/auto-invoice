"""송장 자동화 전 과정을 하나로 묶은 파이프라인.

scripts/run_all.py(터미널)와 scripts/gui.pyw(바탕화면 아이콘)가 이 모듈을
공유한다. 화면에 어떻게 보여줄지는 각자 log 콜백으로 정한다.

    1. 샵마인을 [배송중] 탭으로 맞춘다                  (shopmine/tabs.py)
       -> 다른 탭을 보고 있으면 옮기고, 탭이 닫혀 있으면 메뉴로 다시 연다
    2. 쇼핑몰이 전부 연결돼 있는지 확인한다             (shopmine/connect.py)
       -> 끊긴 곳이 있으면 [연결안된 쇼핑몰 연결재시도]
    3. [배송중] 탭에서 송장수정모드를 켜고, 택배사가 '경동택배' / '직접'인
       주문만 필터로 골라 전부 체크한다                 (shopmine/upload.py)
    4. 체크한 주문만 엑셀로 내보낸다                    (shopmine/export.py)
    5. 공급사에서 송장번호 조회 -> 업로드용 CSV 생성    (orchestrator.py)
       + CJ온스타일은 실제 크롬으로 별도 조회          (cjonstyle_bridge.py)
       + 한 주문번호가 여러 행인 주문은 여기서 제외    (excel_io.py)
    6. CSV를 [발송정보일괄등록(수정용)]으로 [일괄등록]  (shopmine/upload.py)
       -> 그리드의 '송장번호(수정용)' 컬럼이 채워진다
    7. 그 컬럼이 채워진 행만 체크하고 [송장번호수정]    (shopmine/grid.py)
       -> 쇼핑몰까지 실제 반영
    8. 결과에 오류가 없으면 송장수정모드를 끈다

왜 1단계에서 탭부터 맞추나: 3단계 이후의 조작은 전부 '지금 보고 있는 탭'으로
간다. 사람이 신규주문 탭을 열어둔 채로 두면 [송장수정모드 켜기] 좌표도,
Alt+X / Alt+U 도 엉뚱한 화면으로 가버린다.

왜 2단계에서 연결부터 보나: 7단계 [송장번호수정]은 샵마인이 실제로 쇼핑몰에
접속해 송장을 등록한다. 연결이 끊긴 쇼핑몰의 주문은 그때 가서 실패하는데, 그
시점에는 이미 다른 주문들이 반영된 뒤라 되돌리기 번거롭다.

왜 3단계에서 택배사로 거르나: '경동택배'와 '직접(전달)'은 실제 택배사가 아니라
'아직 진짜 송장이 없다'는 표시다. 샵마인도 이 두 값일 때만 송장번호(수정용)를
비워두기 때문에, 이것이 대상 주문을 고르는 기준이자 6단계에서 '일괄등록이
실제로 반영됐는지' 판별하는 근거가 된다.

실제 주문 데이터를 바꾸므로, 확신이 없으면 진행하지 않고 멈춘다. 자세한
안전장치는 shopmine/upload.py 와 README.md 참고.
"""

from __future__ import annotations

import csv
import traceback
from datetime import datetime
from pathlib import Path

from . import cjonstyle_bridge, result_excel
from .orchestrator import run as run_orchestrator
from .shopmine import connect, excel_io, export, grid, tabs, upload

# 내보낸 주문목록 엑셀과 업로드용 CSV는 전부 바탕화면에 저장한다
# (사람이 바로 열어 확인할 수 있어야 해서).
OUTPUT_DIR = Path.home() / "Desktop"

STEPS = 8


class PipelineResult:
    """단계별 결과. 성공 여부와 사람에게 보여줄 요약을 담는다."""

    def __init__(self):
        self.export_path: Path | None = None
        self.csv_path: Path | None = None
        self.picked: int = 0            # 3단계에서 고른 주문 수 (보이는 화면 기준)
        self.lookup_counts: dict = {}
        # 5단계 조회 결과 원본. 6~8단계에서 멈추거나 오류가 나도 이미 끝낸
        # 조회 결과는 그대로 사람에게 넘겨야 해서 들고 있는다
        # (마지막 요약 + 바탕화면 결과 엑셀).
        self.lookup_entries: list = []
        self.lookup_failure_lines: list[str] = []
        self.lookup_report_path: Path | None = None
        self.result_excel_path: Path | None = None
        self.applied_count: int = 0     # [송장번호수정]으로 반영한 건수
        self.apply_status: str = ""     # '오류없음.' 등 결과 창 문구
        self.stopped_reason: str | None = None
        # 자동으로 처리하지 못해 사람이 직접 손봐야 하는 주문들 - (제목, 줄
        # 목록) 묶음. 5단계 로그에 한 번 나오지만 로그가 길어 묻히기 쉬워서,
        # 마지막 요약에 다시 붙이려고 들고 있는다 (report.attention_blocks).
        self.attention_blocks: list[tuple[str, list[str]]] = []

    @property
    def applied(self) -> bool:
        return self.applied_count > 0

    @property
    def lookup_done(self) -> bool:
        """5단계 송장조회까지는 끝났는가 (그 뒤에서 멈췄더라도)."""
        return bool(self.lookup_counts)

    @property
    def apply_ok(self) -> bool:
        return self.applied and upload.result_is_clean(self.apply_status)

    @property
    def applied_bad(self) -> bool:
        return self.applied and not self.apply_ok


def read_csv_order_ids(path) -> list[str]:
    """업로드용 CSV에서 '고객주문번호' 목록을 읽는다."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip()]
    if not rows:
        return []
    header, *data = rows
    try:
        idx = header.index("고객주문번호")
    except ValueError:
        idx = 0
    return [r[idx].strip() for r in data if len(r) > idx and r[idx].strip()]


def ensure_shipping_tab(result: PipelineResult, *, tab="배송중", log=print) -> bool:
    """1단계: 샵마인을 배송중 탭으로 맞춘다.

    다음 단계부터는 '지금 보고 있는 탭'을 조작하므로, 다른 탭이면 아무것도
    하지 않고 멈추는 대신 알아서 그 탭으로 옮긴다.
    """
    log(f"[1/{STEPS}] 샵마인 [{tab}] 탭 확인")
    try:
        tabs.ensure_tab(tab, log=log)
    except tabs.TabError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False
    return True


def ensure_malls_connected(result: PipelineResult, *, log=print) -> bool:
    """2단계: 쇼핑몰이 전부 연결돼 있는지 확인하고, 끊긴 곳은 다시 연결한다.

    끊긴 채로 진행하면 7단계 [송장번호수정]에서 그 쇼핑몰 주문만 조용히
    실패하므로, 확인하지 못하면 아무것도 하지 않고 멈춘다.
    """
    log(f"[2/{STEPS}] 쇼핑몰 연결 상태 확인")
    try:
        connect.ensure_connected(log=log)
    except connect.ConnectError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False
    return True


def select_and_export(result: PipelineResult, *, tab="배송중", log=print) -> bool:
    """3~4단계: 고칠 주문만 체크하고 그 주문만 엑셀로 내보낸다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result.export_path = OUTPUT_DIR / f"주문목록_{stamp}.xls"
    result.csv_path = OUTPUT_DIR / f"송장업로드_{stamp}.csv"

    log("")
    log(f"[3/{STEPS}] 배송중 탭에서 송장을 고칠 주문 고르기 "
        f"({' / '.join(upload.TARGET_COURIERS)})")
    try:
        upload.ensure_edit_mode(log=log)
        main = upload.main_window()
        grid.clear_all_checks(main, log=log)
        result.picked = upload.select_target_orders(upload.TARGET_COURIERS, log=log)
    except (upload.UploadError, grid.GridError) as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False

    log("")
    log(f"[4/{STEPS}] 체크한 주문만 엑셀로 내보내기")
    try:
        export.export_to(result.export_path, tab_title=tab, log=log)
    except export.ExportError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False
    return True


def lookup_tracking(result: PipelineResult, *, limit=None, headless=False,
                    skip_cjonstyle=False, log=print) -> None:
    """5단계: 공급사에서 송장번호를 조회해 업로드용 CSV를 만든다."""
    log("")
    log(f"[5/{STEPS}] 공급사에서 송장번호 조회")
    report = run_orchestrator(str(result.export_path), str(result.csv_path),
                              limit=limit, headless=headless)

    # CJ온스타일은 Playwright 로그인이 막혀 있어 orchestrator가 전부 스킵한다.
    # 실제 크롬 브라우저로 따로 조회해서 합친다. (--limit 로 소량만 볼 때는
    # 이 느린 경로를 타지 않는다.)
    if not skip_cjonstyle and limit is None:
        try:
            added = cjonstyle_bridge.process_orders(
                report, str(result.export_path), str(result.csv_path), log=log)
            if added:
                log(f"  CJ온스타일 {added}건 추가됨")
        except Exception as e:  # noqa: BLE001
            log(f"  CJ온스타일 처리 건너뜀: {e}")

    result.lookup_counts = report.summary()
    result.lookup_entries = list(report.entries)
    result.lookup_failure_lines = report.failure_lines()
    result.attention_blocks = report.attention_blocks()
    log(f"  성공 {result.lookup_counts['success']} / "
        f"실패 {result.lookup_counts['fail']} / 스킵 {result.lookup_counts['skip']}")
    for line in report.failure_lines():
        log(f"  {line}")
    for title, lines in result.attention_blocks:
        log(f"  [{title}]")
        for line in lines:
            log(f"  {line}")
    result.lookup_report_path = report.save()
    log(f"  상세 리포트: {result.lookup_report_path}")


def apply_csv(csv_path, order_ids, *, max_apply=100, expected_filled=None,
              stop_before_apply=False, log=print,
              result: PipelineResult | None = None) -> PipelineResult:
    """6~8단계. 엑셀 내보내기/조회가 이미 끝난 상태에서 실행한다.

    expected_filled: 일괄등록으로 실제로 채워질 **그리드 행** 수. 한 주문번호가
    그리드에 여러 행으로 있으면 CSV 한 줄이 그 행을 전부 채우기 때문에 CSV 줄
    수와 다를 수 있다(excel_io.resolve_duplicate_orders 참고). 모르면 CSV 줄
    수를 그대로 쓴다.
    """
    result = result or PipelineResult()
    result.csv_path = Path(csv_path)
    rows = len(order_ids)
    expected = expected_filled if expected_filled is not None else rows

    if expected > max_apply:
        result.stopped_reason = (
            f"반영 대상이 {expected}건으로 상한({max_apply}건)을 넘습니다. "
            "CSV는 만들어 두었으니 확인 후 상한을 올려 다시 실행하세요.")
        log(f"중단: {result.stopped_reason}")
        return result

    log("")
    log(f"[6/{STEPS}] CSV 일괄등록 ({rows}건)")
    try:
        upload.ensure_edit_mode(log=log)
        upload.bulk_register(csv_path, log=log)
    except (upload.UploadError, grid.GridError) as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return result

    if stop_before_apply:
        log("")
        log("6단계까지 완료했습니다. 화면에서 '송장번호(수정용)' 컬럼을 확인한 뒤")
        log("채워진 행을 체크하고 [송장번호수정] 버튼을 직접 눌러주세요.")
        return result

    log("")
    log(f"[7/{STEPS}] 송장이 채워진 행만 체크하고 [송장번호수정]")
    try:
        picked = upload.select_registered_rows(log=log)
        if picked > expected:
            raise upload.UploadError(
                f"송장이 채워진 행이 {picked}건으로 예상({expected}건)보다 많습니다. "
                "필터가 제대로 걸리지 않았거나, 한 주문번호가 그리드 여러 행으로 "
                "나뉜 주문이 섞였을 수 있어 아무것도 반영하지 않았습니다.")
        if picked < expected:
            log(f"  경고: {expected}건이 채워져야 하는데 화면에서 채워진 건 {picked}건입니다 "
                "- 채워진 건만 반영합니다.")
        result.apply_status = upload.apply_tracking(picked, log=log)
        result.applied_count = picked
    except (upload.UploadError, grid.GridError) as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        upload.cleanup_stray_dialogs()
        return result

    log("")
    log(f"[8/{STEPS}] 마무리")
    if not result.apply_ok:
        log(f"  결과에 오류가 있어 송장수정모드를 켜둔 채로 둡니다: {result.apply_status!r}")
        log("  샵마인 화면에서 직접 확인해주세요.")
        return result
    try:
        upload.wait_until_idle(log=log)
        grid.clear_all_checks(upload.main_window(), log=log)
        upload.disable_edit_mode(log=log)
    except (upload.UploadError, grid.GridError) as e:
        log(f"  경고: 마무리 정리를 끝내지 못했습니다 - {e}")
    return result


def run_full(*, limit=None, max_apply=100, tab="배송중", stop_before_apply=False,
             headless=False, skip_cjonstyle=False, log=print) -> PipelineResult:
    """1~8단계 전체."""
    result = PipelineResult()
    if not ensure_shipping_tab(result, tab=tab, log=log):
        return result
    if not ensure_malls_connected(result, log=log):
        return result
    if not select_and_export(result, tab=tab, log=log):
        return result

    lookup_tracking(result, limit=limit, headless=headless,
                    skip_cjonstyle=skip_cjonstyle, log=log)
    # 조회가 끝난 뒤로는 무슨 일이 생기든 결과를 들고 돌아온다. 반영 단계에서
    # 멈추든 예상 못 한 오류로 죽든, 이미 끝낸 송장조회 결과는 사람에게 그대로
    # 넘겨줘야 한다 - 결과 엑셀로 남기고 마지막 요약에도 싣는다.
    try:
        _apply_after_lookup(result, max_apply=max_apply,
                            stop_before_apply=stop_before_apply, log=log)
    except Exception as e:  # noqa: BLE001 - 조회 결과를 잃지 않는 것이 우선
        result.stopped_reason = f"예상치 못한 오류: {e}"
        log("")
        log(f"중단: {result.stopped_reason}")
        log(traceback.format_exc())
    finally:
        save_result_excel(result, log=log)
    return result


def _apply_after_lookup(result: PipelineResult, *, max_apply, stop_before_apply,
                        log=print) -> None:
    """조회가 끝난 뒤의 6~8단계. 결과는 전부 result에 쌓는다."""
    if result.lookup_counts.get("success", 0) == 0:
        result.stopped_reason = "조회 성공 건이 없어 종료했습니다 (샵마인은 건드리지 않음)."
        log("")
        log(result.stopped_reason)
        return

    order_ids = read_csv_order_ids(result.csv_path)
    log(f"  업로드용 CSV: {result.csv_path} ({len(order_ids)}건)")
    # CSV 한 줄이 그리드 여러 행을 채우는 주문이 있을 수 있어, 5단계에서 몇
    # 행이 채워져야 맞는지는 내보내기 엑셀의 행 수로 센다.
    expected = excel_io.count_export_rows(str(result.export_path), order_ids)
    if expected != len(order_ids):
        log(f"  (그리드 기준 {expected}행 - 한 주문번호가 여러 행인 주문 포함)")
    apply_csv(result.csv_path, order_ids, max_apply=max_apply,
              expected_filled=expected, stop_before_apply=stop_before_apply,
              log=log, result=result)


def run_from_csv(csv_path, *, max_apply=100, tab="배송중",
                 stop_before_apply=False, log=print) -> PipelineResult:
    """이미 만들어둔 CSV로 6~8단계만 (탭 확인과 쇼핑몰 연결 확인은 이때도 한다)."""
    result = PipelineResult()
    csv_path = Path(csv_path)
    if not csv_path.exists():
        result.stopped_reason = f"CSV가 없습니다 - {csv_path}"
        log(f"중단: {result.stopped_reason}")
        return result
    order_ids = read_csv_order_ids(csv_path)
    log(f"  CSV: {csv_path} ({len(order_ids)}건)")
    if not order_ids:
        result.stopped_reason = "CSV에 주문번호가 없습니다."
        log(f"중단: {result.stopped_reason}")
        return result
    if not ensure_shipping_tab(result, tab=tab, log=log):
        return result
    if not ensure_malls_connected(result, log=log):
        return result
    return apply_csv(csv_path, order_ids, max_apply=max_apply,
                     stop_before_apply=stop_before_apply, log=log, result=result)


def summarize(result: PipelineResult) -> str:
    """실행 결과를 사람이 읽을 한 덩어리 요약으로.

    자동으로 처리하지 못해 사람이 직접 손봐야 하는 주문(아직 지원하지 않는
    사이트, 취소/품절)은 5단계 로그에 이미 한 번 나오지만 그 위로 로그가 길게
    쌓여 묻히기 쉬워서, 맨 마지막 요약에 수령인 이름과 함께 다시 붙인다.
    """
    return "\n".join([_apply_summary(result), *_lookup_summary(result),
                      *_attention_summary(result)])


def _lookup_summary(result: PipelineResult) -> list[str]:
    """송장조회 결과. 반영까지 못 가고 멈췄어도 조회가 끝났으면 보여준다.

    멈춘 실행에서 '중단됨' 한 줄만 남으면 이미 다 해둔 조회가 없던 일처럼
    보인다. 실제로는 업로드용 CSV와 결과 엑셀이 만들어져 있어서 사람이 그걸로
    이어서 처리하면 되므로, 그 경로까지 같이 알려준다.
    """
    if not result.lookup_done:
        return []
    counts = result.lookup_counts
    stopped = bool(result.stopped_reason) and not result.applied
    head = "송장조회는 끝냈습니다" if stopped else "송장조회"
    lines = ["",
             f"{head}: 성공 {counts.get('success', 0)} / "
             f"실패 {counts.get('fail', 0)} / 스킵 {counts.get('skip', 0)}"]
    if result.csv_path and Path(result.csv_path).exists():
        lines.append(f"  업로드용 CSV: {result.csv_path}")
    if result.result_excel_path:
        lines.append(f"  조회 결과 엑셀: {result.result_excel_path}")
    if result.lookup_failure_lines:
        lines.append(f"[조회 실패] {len(result.lookup_failure_lines)}건 (직접 확인해주세요)")
        lines.extend(result.lookup_failure_lines)
    return lines


def save_result_excel(result: PipelineResult, *, log=print) -> None:
    """송장조회 결과를 바탕화면 엑셀로 남긴다 (조회를 했으면 항상).

    run_full의 finally에서 부른다 - 반영 단계에서 멈추든 오류로 죽든, 이미
    끝낸 조회 결과는 파일로 남아야 사람이 이어서 처리할 수 있다.
    """
    if not result.lookup_entries:
        return
    log("")
    result.result_excel_path = result_excel.save_run_result(
        result.lookup_entries, result.lookup_counts,
        applied_label=_applied_label(result),
        apply_note=_apply_summary(result),
        paths=(("내보낸 주문목록", result.export_path),
               ("업로드용 CSV", result.csv_path),
               ("상세 로그", result.lookup_report_path)),
        out_dir=OUTPUT_DIR, log=log)


def _applied_label(result: PipelineResult) -> str:
    """성공 건의 '샵마인 반영' 칸 문구.

    주문 하나하나가 반영됐는지는 알 수 없다 - 샵마인 결과 창이 건수와 오류
    여부만 주기 때문에, 실행 단위 결과를 성공 건 전체에 그대로 붙인다.
    """
    if result.apply_ok:
        return "반영완료"
    if result.applied:
        return "반영결과 확인필요"
    if result.stopped_reason:
        return "미반영 (중단)"
    return "미반영"


def _attention_summary(result: PipelineResult) -> list[str]:
    lines: list[str] = []
    for title, entries in result.attention_blocks:
        lines.append("")
        lines.append(f"[{title}] {len(entries)}건")
        lines.extend(entries)
    return lines


def _apply_summary(result: PipelineResult) -> str:
    """샵마인 반영 결과 한 줄."""
    if result.stopped_reason and not result.applied:
        return f"중단됨: {result.stopped_reason}"
    if not result.applied:
        return "반영한 건이 없습니다."
    if result.apply_ok:
        return f"반영 완료 {result.applied_count}건 (결과: {result.apply_status})"
    return (f"반영 시도 {result.applied_count}건 - 결과 창에 오류가 있습니다: "
            f"{result.apply_status or '(문구 없음)'}\n"
            "샵마인 화면에서 직접 확인해주세요.")
