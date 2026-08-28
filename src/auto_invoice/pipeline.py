"""송장 자동화 전 과정을 하나로 묶은 파이프라인.

scripts/run_all.py(터미널)와 scripts/gui.pyw(바탕화면 아이콘)가 이 모듈을
공유한다. 화면에 어떻게 보여줄지는 각자 log 콜백으로 정한다.

    1. [배송중] 탭에서 송장수정모드를 켜고, 택배사가 '경동택배' / '직접'인
       주문만 필터로 골라 전부 체크한다                 (shopmine/upload.py)
    2. 체크한 주문만 엑셀로 내보낸다                    (shopmine/export.py)
    3. 공급사에서 송장번호 조회 -> 업로드용 CSV 생성    (orchestrator.py)
       + CJ온스타일은 실제 크롬으로 별도 조회          (cjonstyle_bridge.py)
       + 한 주문번호가 여러 행인 주문은 여기서 제외    (excel_io.py)
    4. CSV를 [발송정보일괄등록(수정용)]으로 [일괄등록]  (shopmine/upload.py)
       -> 그리드의 '송장번호(수정용)' 컬럼이 채워진다
    5. 그 컬럼이 채워진 행만 체크하고 [송장번호수정]    (shopmine/grid.py)
       -> 쇼핑몰까지 실제 반영
    6. 결과에 오류가 없으면 송장수정모드를 끈다

왜 1단계에서 택배사로 거르나: '경동택배'와 '직접(전달)'은 실제 택배사가 아니라
'아직 진짜 송장이 없다'는 표시다. 샵마인도 이 두 값일 때만 송장번호(수정용)를
비워두기 때문에, 이것이 대상 주문을 고르는 기준이자 5단계에서 '일괄등록이
실제로 반영됐는지' 판별하는 근거가 된다.

실제 주문 데이터를 바꾸므로, 확신이 없으면 진행하지 않고 멈춘다. 자세한
안전장치는 shopmine/upload.py 와 README.md 참고.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from . import cjonstyle_bridge
from .orchestrator import run as run_orchestrator
from .shopmine import excel_io, export, grid, upload

# 내보낸 주문목록 엑셀과 업로드용 CSV는 전부 바탕화면에 저장한다
# (사람이 바로 열어 확인할 수 있어야 해서).
OUTPUT_DIR = Path.home() / "Desktop"

STEPS = 6


class PipelineResult:
    """단계별 결과. 성공 여부와 사람에게 보여줄 요약을 담는다."""

    def __init__(self):
        self.export_path: Path | None = None
        self.csv_path: Path | None = None
        self.picked: int = 0            # 1단계에서 고른 주문 수 (보이는 화면 기준)
        self.lookup_counts: dict = {}
        self.applied_count: int = 0     # [송장번호수정]으로 반영한 건수
        self.apply_status: str = ""     # '오류없음.' 등 결과 창 문구
        self.stopped_reason: str | None = None
        # 자동으로 처리하지 못해 사람이 직접 손봐야 하는 주문들 - (제목, 줄
        # 목록) 묶음. 3단계 로그에 한 번 나오지만 로그가 길어 묻히기 쉬워서,
        # 마지막 요약에 다시 붙이려고 들고 있는다 (report.attention_blocks).
        self.attention_blocks: list[tuple[str, list[str]]] = []

    @property
    def applied(self) -> bool:
        return self.applied_count > 0

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


def select_and_export(result: PipelineResult, *, tab="배송중", log=print) -> bool:
    """1~2단계: 고칠 주문만 체크하고 그 주문만 엑셀로 내보낸다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result.export_path = OUTPUT_DIR / f"주문목록_{stamp}.xls"
    result.csv_path = OUTPUT_DIR / f"송장업로드_{stamp}.csv"

    log(f"[1/{STEPS}] 배송중 탭에서 송장을 고칠 주문 고르기 "
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
    log(f"[2/{STEPS}] 체크한 주문만 엑셀로 내보내기")
    try:
        export.export_to(result.export_path, tab_title=tab, log=log)
    except export.ExportError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False
    return True


def lookup_tracking(result: PipelineResult, *, limit=None, headless=False,
                    skip_cjonstyle=False, log=print) -> None:
    """3단계: 공급사에서 송장번호를 조회해 업로드용 CSV를 만든다."""
    log("")
    log(f"[3/{STEPS}] 공급사에서 송장번호 조회")
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
    result.attention_blocks = report.attention_blocks()
    log(f"  성공 {result.lookup_counts['success']} / "
        f"실패 {result.lookup_counts['fail']} / 스킵 {result.lookup_counts['skip']}")
    for line in report.failure_lines():
        log(f"  {line}")
    for title, lines in result.attention_blocks:
        log(f"  [{title}]")
        for line in lines:
            log(f"  {line}")
    log(f"  상세 리포트: {report.save()}")


def apply_csv(csv_path, order_ids, *, max_apply=100, expected_filled=None,
              stop_before_apply=False, log=print,
              result: PipelineResult | None = None) -> PipelineResult:
    """4~6단계. 엑셀 내보내기/조회가 이미 끝난 상태에서 실행한다.

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
    log(f"[4/{STEPS}] CSV 일괄등록 ({rows}건)")
    try:
        upload.ensure_edit_mode(log=log)
        upload.bulk_register(csv_path, log=log)
    except (upload.UploadError, grid.GridError) as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return result

    if stop_before_apply:
        log("")
        log("4단계까지 완료했습니다. 화면에서 '송장번호(수정용)' 컬럼을 확인한 뒤")
        log("채워진 행을 체크하고 [송장번호수정] 버튼을 직접 눌러주세요.")
        return result

    log("")
    log(f"[5/{STEPS}] 송장이 채워진 행만 체크하고 [송장번호수정]")
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
    log(f"[6/{STEPS}] 마무리")
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
    """1~6단계 전체."""
    result = PipelineResult()
    if not select_and_export(result, tab=tab, log=log):
        return result

    lookup_tracking(result, limit=limit, headless=headless,
                    skip_cjonstyle=skip_cjonstyle, log=log)
    if result.lookup_counts.get("success", 0) == 0:
        result.stopped_reason = "조회 성공 건이 없어 종료했습니다 (샵마인은 건드리지 않음)."
        log("")
        log(result.stopped_reason)
        return result

    order_ids = read_csv_order_ids(result.csv_path)
    log(f"  업로드용 CSV: {result.csv_path} ({len(order_ids)}건)")
    # CSV 한 줄이 그리드 여러 행을 채우는 주문이 있을 수 있어, 5단계에서 몇
    # 행이 채워져야 맞는지는 내보내기 엑셀의 행 수로 센다.
    expected = excel_io.count_export_rows(str(result.export_path), order_ids)
    if expected != len(order_ids):
        log(f"  (그리드 기준 {expected}행 - 한 주문번호가 여러 행인 주문 포함)")
    return apply_csv(result.csv_path, order_ids, max_apply=max_apply,
                     expected_filled=expected, stop_before_apply=stop_before_apply,
                     log=log, result=result)


def run_from_csv(csv_path, *, max_apply=100, stop_before_apply=False,
                 log=print) -> PipelineResult:
    """이미 만들어둔 CSV로 4~6단계만."""
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
    return apply_csv(csv_path, order_ids, max_apply=max_apply,
                     stop_before_apply=stop_before_apply, log=log, result=result)


def summarize(result: PipelineResult) -> str:
    """실행 결과를 사람이 읽을 한 덩어리 요약으로.

    자동으로 처리하지 못해 사람이 직접 손봐야 하는 주문(아직 지원하지 않는
    사이트, 취소/품절)은 3단계 로그에 이미 한 번 나오지만 그 위로 로그가 길게
    쌓여 묻히기 쉬워서, 맨 마지막 요약에 수령인 이름과 함께 다시 붙인다.
    """
    return "\n".join([_apply_summary(result), *_attention_summary(result)])


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
