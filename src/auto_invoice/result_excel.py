"""송장조회 결과를 사람이 바로 열어볼 엑셀로 남긴다 (바탕화면).

왜 필요한가: 6~8단계(샵마인 반영)에서 멈춰도 5단계 송장조회는 이미 끝나 있는
경우가 대부분이다. 그때 조회 결과가 logs/run_*.json 에만 남으면 사람이 JSON을
열어봐야 하고, 멈춘 뒤 어느 주문을 직접 처리해야 하는지 한눈에 안 들어온다.
그래서 같은 내용을 바탕화면 엑셀로도 남긴다 - 반영까지 성공한 실행이든,
중간에 멈춘 실행이든 조회를 한 번이라도 했으면 항상 만든다.

정렬은 파일 순서가 아니라 '사람 손이 먼저 가야 하는 순서'로 한다
(실패 -> 미지원 사이트 -> 취소/품절 -> 스킵 -> 성공). 성공 건은 이미
업로드용 CSV로 처리되므로 맨 뒤여도 되고, 확인이 필요한 건이 위로 올라온다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import ReportEntry

# 사람이 바로 열어볼 수 있게 바탕화면에 둔다 (내보내기 엑셀/업로드 CSV와 같은 곳).
DEFAULT_DIR = Path.home() / "Desktop"

SHEET_NAME = "송장조회결과"
SUMMARY_SHEET_NAME = "요약"

HEADERS = ["결과", "마켓 주문번호", "수령인", "택배사", "송장번호", "샵마인 반영", "사유"]
COLUMN_WIDTHS = [14, 22, 10, 12, 22, 16, 60]

STATUS_LABELS = {"success": "성공", "fail": "실패", "skip": "스킵"}
CATEGORY_LABELS = {"unsupported_site": "미지원 사이트", "cancelled": "취소/품절"}

# 위에 올라올수록 사람이 먼저 봐야 하는 결과.
_SORT_ORDER = ["실패", "미지원 사이트", "취소/품절", "스킵", "성공"]

_HEADER_FILL = PatternFill("solid", fgColor="E8EAED")
_ROW_FILLS = {
    "실패": PatternFill("solid", fgColor="FCE8E6"),
    "미지원 사이트": PatternFill("solid", fgColor="FEF7E0"),
    "취소/품절": PatternFill("solid", fgColor="FEF7E0"),
}
# 주문번호/송장번호는 자릿수가 길어 엑셀이 지수표기(2.21E+13)로 바꿔버린다.
_TEXT_FORMAT = "@"


def result_label(entry: ReportEntry) -> str:
    """엑셀 '결과' 칸에 쓸 한 마디.

    사람이 직접 처리해야 하는 스킵(미지원 사이트/취소·품절)은 그냥 '스킵'과
    구분해서 보여준다 - 기다리면 되는 건과 손을 대야 하는 건이 다르다.
    """
    if entry.category:
        return CATEGORY_LABELS.get(entry.category, entry.category)
    return STATUS_LABELS.get(entry.status, entry.status)


def _sort_key(entry: ReportEntry) -> int:
    label = result_label(entry)
    return _SORT_ORDER.index(label) if label in _SORT_ORDER else len(_SORT_ORDER)


def write_result_excel(
    entries: list[ReportEntry],
    path: str | Path,
    *,
    applied_label: str = "",
    summary: list[tuple[str, str]] | None = None,
) -> Path:
    """조회 결과 목록을 엑셀로 저장하고 저장한 경로를 돌려준다.

    applied_label: 성공 건의 '샵마인 반영' 칸에 넣을 문구. 반영까지 갔는지는
    주문 단위로 알 수 없고(샵마인 결과 창은 건수만 준다) 실행 단위로만 알 수
    있어서, 실행 결과를 그대로 성공 건에 붙인다.
    summary: 요약 시트에 넣을 (항목, 값) 줄들.
    """
    path = Path(path)
    wb = Workbook()
    _write_entries_sheet(wb.active, entries, applied_label)
    if summary:
        _write_summary_sheet(wb.create_sheet(SUMMARY_SHEET_NAME), summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _write_entries_sheet(ws, entries: list[ReportEntry], applied_label: str) -> None:
    ws.title = SHEET_NAME
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")

    for entry in sorted(entries, key=_sort_key):
        label = result_label(entry)
        ws.append([
            label,
            entry.order_id,
            entry.recipient_name or "",
            entry.courier or "",
            entry.tracking_no or "",
            applied_label if entry.status == "success" else "",
            entry.reason or "",
        ])
        row = ws[ws.max_row]
        for cell in (row[1], row[4]):  # 마켓 주문번호, 송장번호
            cell.number_format = _TEXT_FORMAT
        fill = _ROW_FILLS.get(label)
        if fill:
            for cell in row:
                cell.fill = fill
        row[6].alignment = Alignment(wrap_text=True, vertical="top")

    for i, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(ws.max_row, 1)}"


def _write_summary_sheet(ws, summary: list[tuple[str, str]]) -> None:
    for name, value in summary:
        ws.append([name, value])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        ws.cell(row=ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 80


def save_run_result(entries, counts: dict, *, applied_label: str, apply_note: str = "",
                    paths=(), out_dir: Path | None = None, log=print) -> Path | None:
    """한 번의 실행 결과를 '송장조회결과_시각.xlsx'로 저장한다.

    paths: 요약 시트에 남길 (이름, 경로) 목록. 값이 없는 항목은 뺀다.
    저장에 실패해도 실행 결과 자체를 덮으면 안 되므로, 예외는 경고 한 줄로
    남기고 None을 돌려준다 - 조회 결과는 logs/run_*.json에도 남아 있다.
    """
    if not entries:
        return None
    out_dir = out_dir or DEFAULT_DIR
    path = out_dir / f"송장조회결과_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    summary = [
        ("실행 시각", f"{datetime.now():%Y-%m-%d %H:%M:%S}"),
        ("조회 성공", f"{counts.get('success', 0)}건"),
        ("조회 실패", f"{counts.get('fail', 0)}건"),
        ("조회 스킵", f"{counts.get('skip', 0)}건"),
    ]
    if apply_note:
        summary.append(("샵마인 반영", apply_note))
    summary.extend((name, str(value)) for name, value in paths if value)
    try:
        saved = write_result_excel(entries, path, applied_label=applied_label,
                                   summary=summary)
    except Exception as e:  # noqa: BLE001 - 결과 저장 실패가 실행 결과를 덮으면 안 된다
        log(f"경고: 결과 엑셀을 저장하지 못했습니다 - {e}")
        return None
    log(f"조회 결과 엑셀: {saved}")
    return saved
