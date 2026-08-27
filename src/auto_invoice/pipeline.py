"""송장 자동화 전 과정을 하나로 묶은 파이프라인.

scripts/run_all.py(터미널)와 scripts/gui.pyw(바탕화면 아이콘)가 이 모듈을
공유한다. 화면에 어떻게 보여줄지는 각자 log 콜백으로 정한다.

    1. 샵마인 [배송중] 탭에서 주문 목록 엑셀 내보내기   (shopmine/export.py)
    2. 공급사에서 송장번호 조회 -> 업로드용 CSV 생성    (orchestrator.py)
       + CJ온스타일은 실제 크롬으로 별도 조회          (cjonstyle_bridge.py)
    3. CSV를 [발송정보일괄등록(수정용)]으로 업로드      (shopmine/upload.py)
    4. [일괄등록] - 송장번호(수정용) 컬럼에 반영
    5. [송장번호수정] - 쇼핑몰까지 실제 반영

실제 주문 데이터를 바꾸므로, 확신이 없으면 진행하지 않고 멈춘다. 자세한
안전장치는 shopmine/upload.py 와 README.md 참고.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from . import cjonstyle_bridge
from .orchestrator import run as run_orchestrator
from .shopmine import export, upload

WORK_DIR = Path(__file__).resolve().parent.parent.parent / "work"


class PipelineResult:
    """단계별 결과. 성공 여부와 사람에게 보여줄 요약을 담는다."""

    def __init__(self):
        self.export_path: Path | None = None
        self.csv_path: Path | None = None
        self.lookup_counts: dict = {}
        self.applied: list[tuple[str, str]] = []
        self.stopped_reason: str | None = None

    @property
    def applied_ok(self) -> list[str]:
        return [o for o, s in self.applied if s.startswith("오류없음")]

    @property
    def applied_bad(self) -> list[tuple[str, str]]:
        return [(o, s) for o, s in self.applied if not s.startswith("오류없음")]


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


def export_and_lookup(result: PipelineResult, *, limit=None, tab="배송중",
                      headless=False, skip_cjonstyle=False, log=print) -> bool:
    """1~2단계. 성공하면 True."""
    WORK_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result.export_path = WORK_DIR / f"주문목록_{stamp}.xls"
    result.csv_path = WORK_DIR / f"송장업로드_{stamp}.csv"

    log(f"[1/5] 샵마인 [{tab}] 탭에서 주문 목록 내보내기")
    try:
        export.export_to(result.export_path, tab_title=tab, log=log)
    except export.ExportError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return False

    log("")
    log("[2/5] 공급사에서 송장번호 조회")
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
    log(f"  성공 {result.lookup_counts['success']} / "
        f"실패 {result.lookup_counts['fail']} / 스킵 {result.lookup_counts['skip']}")
    for line in report.failure_lines():
        log(f"  {line}")
    log(f"  상세 리포트: {report.save()}")
    return True


def apply_csv(csv_path, order_ids, *, max_apply=30, stop_before_apply=False,
              log=print, result: PipelineResult | None = None) -> PipelineResult:
    """3~5단계. 엑셀 내보내기/조회가 이미 끝난 상태에서 실행한다."""
    result = result or PipelineResult()
    result.csv_path = Path(csv_path)
    rows = len(order_ids)

    if rows > max_apply:
        result.stopped_reason = (
            f"반영 대상이 {rows}건으로 상한({max_apply}건)을 넘습니다. "
            "CSV는 만들어 두었으니 확인 후 상한을 올려 다시 실행하세요.")
        log(f"중단: {result.stopped_reason}")
        return result

    log("")
    log("[3/5] 샵마인 송장수정모드 켜고 업로드 창 열기")
    try:
        upload.ensure_edit_mode(log=log)
        log("")
        log(f"[4/5] CSV 일괄등록 ({rows}건)")
        upload.bulk_register(csv_path, log=log)
    except upload.UploadError as e:
        result.stopped_reason = str(e)
        log(f"중단: {e}")
        return result

    if stop_before_apply:
        log("")
        log("4단계까지 완료했습니다. 화면에서 '송장번호(수정용)' 컬럼을 확인한 뒤")
        log("[송장번호수정] 버튼을 직접 눌러주세요.")
        return result

    log("")
    log(f"[5/5] [송장번호수정]으로 쇼핑몰까지 반영 ({rows}건, 한 건씩)")
    result.applied = upload.apply_one_by_one(order_ids, log=log)
    try:
        upload.filter_grid("", log=log)      # 목록 원상복구
    except upload.UploadError as e:
        log(f"  경고: 목록 필터를 되돌리지 못했습니다 - {e}")
    return result


def run_full(*, limit=None, max_apply=30, tab="배송중", stop_before_apply=False,
             headless=False, skip_cjonstyle=False, log=print) -> PipelineResult:
    """1~5단계 전체."""
    result = PipelineResult()
    if not export_and_lookup(result, limit=limit, tab=tab, headless=headless,
                             skip_cjonstyle=skip_cjonstyle, log=log):
        return result

    if result.lookup_counts.get("success", 0) == 0:
        result.stopped_reason = "조회 성공 건이 없어 종료했습니다 (샵마인은 건드리지 않음)."
        log("")
        log(result.stopped_reason)
        return result

    order_ids = read_csv_order_ids(result.csv_path)
    log(f"  업로드용 CSV: {result.csv_path} ({len(order_ids)}건)")
    return apply_csv(result.csv_path, order_ids, max_apply=max_apply,
                     stop_before_apply=stop_before_apply, log=log, result=result)


def run_from_csv(csv_path, *, max_apply=30, stop_before_apply=False,
                 log=print) -> PipelineResult:
    """이미 만들어둔 CSV로 3~5단계만."""
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
    """실행 결과를 사람이 읽을 한 덩어리 요약으로."""
    if result.stopped_reason and not result.applied:
        return f"중단됨: {result.stopped_reason}"
    ok, bad = result.applied_ok, result.applied_bad
    lines = [f"반영 성공 {len(ok)}건 / 실패 {len(bad)}건"]
    for o, s in bad:
        lines.append(f"  실패 {o}: {s}")
    if bad:
        lines.append("실패 건은 샵마인에서 직접 확인해주세요.")
    return "\n".join(lines)
