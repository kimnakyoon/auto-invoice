"""송장조회 결과를 사람이 바로 열어볼 엑셀로 남긴다 (바탕화면).

왜 필요한가: 6~8단계(샵마인 반영)에서 멈춰도 5단계 송장조회는 이미 끝나 있는
경우가 대부분이다. 그때 조회 결과가 logs/run_*.json 에만 남으면 사람이 JSON을
열어봐야 하고, 멈춘 뒤 어느 주문을 직접 처리해야 하는지 한눈에 안 들어온다.
그래서 같은 내용을 바탕화면 엑셀로도 남긴다 - 반영까지 성공한 실행이든,
중간에 멈춘 실행이든 조회를 한 번이라도 했으면 항상 만든다.

정렬은 파일 순서가 아니라 '사람 손이 먼저 가야 하는 순서'로 한다
(실패 -> 미지원 사이트 -> 취소/품절 -> 스킵 -> 성공). 성공 건은 이미
업로드용 CSV로 처리되므로 맨 뒤여도 되고, 확인이 필요한 건이 위로 올라온다.

보기 편하라고 넣은 것들: 맨 위에 실행 시각과 결과별 건수 한 줄, '결과' 칸은
색깔 배지, 줄 전체는 같은 계열 옅은 색, 결과가 바뀌는 자리에는 굵은 가로선.
파일을 열자마자 '확인해야 할 게 몇 건인지'가 먼저 보이는 게 목적이다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import ReportEntry

# 사람이 바로 열어볼 수 있게 바탕화면에 둔다 (내보내기 엑셀/업로드 CSV와 같은 곳).
DEFAULT_DIR = Path.home() / "Desktop"

SHEET_NAME = "송장조회결과"
SUMMARY_SHEET_NAME = "요약"

HEADERS = ["결과", "마켓 주문번호", "수령인", "택배사", "송장번호", "샵마인 반영", "사유"]
COLUMN_WIDTHS = [16, 22, 10, 12, 22, 16, 62]

STATUS_LABELS = {"success": "성공", "fail": "실패", "skip": "스킵"}
CATEGORY_LABELS = {"unsupported_site": "미지원 사이트", "cancelled": "취소/품절"}

# 위에 올라올수록 사람이 먼저 봐야 하는 결과.
_SORT_ORDER = ["실패", "미지원 사이트", "취소/품절", "스킵", "성공"]

# 결과별 색: (배지 바탕, 배지 글씨, 줄 바탕). 배지는 진하게, 줄은 옅게 해서
# 스크롤할 때 색 덩어리만 보고도 어디까지가 같은 결과인지 알 수 있게 한다.
_COLORS = {
    "실패": ("D93025", "FFFFFF", "FCE8E6"),
    "미지원 사이트": ("E8710A", "FFFFFF", "FEF0E0"),
    "취소/품절": ("9334E6", "FFFFFF", "F3E8FD"),
    "스킵": ("5F6368", "FFFFFF", "F1F3F4"),
    "성공": ("188038", "FFFFFF", "E6F4EA"),
}
_DEFAULT_COLOR = ("5F6368", "FFFFFF", "FFFFFF")

# 결과별로 사람이 뭘 해야 하는지 한 마디 - 요약 시트에서 쓴다.
_ACTIONS = {
    "실패": "직접 확인 필요",
    "미지원 사이트": "공급사 사이트에서 직접 조회 (어댑터 없음)",
    "취소/품절": "취소/품절 주문인지 확인",
    "스킵": "그냥 두면 되는 건 (송장 미발급 등)",
    "성공": "송장 확보 - 업로드용 CSV로 반영",
}

_TITLE_FILL = PatternFill("solid", fgColor="202124")
_HEADER_FILL = PatternFill("solid", fgColor="E8EAED")
_THIN = Side(style="thin", color="D0D3D7")
_GROUP_LINE = Side(style="medium", color="80868B")
_CELL_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

# 주문번호/송장번호는 자릿수가 길어 엑셀이 지수표기(2.21E+13)로 바꿔버린다.
_TEXT_FORMAT = "@"

# 제목 2줄(제목/건수) 다음이 헤더, 그 다음부터 데이터.
_HEADER_ROW = 3


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


def label_counts(entries: list[ReportEntry]) -> list[tuple[str, int]]:
    """결과별 건수를 '사람 손이 먼저 가는 순서'로 돌려준다 (0건은 빼고)."""
    counts: dict[str, int] = {}
    for entry in entries:
        label = result_label(entry)
        counts[label] = counts.get(label, 0) + 1
    ordered = [(label, counts.pop(label)) for label in _SORT_ORDER if label in counts]
    ordered.extend(sorted(counts.items()))
    return ordered


def write_result_excel(
    entries: list[ReportEntry],
    path: str | Path,
    *,
    applied_label: str = "",
    summary: list[tuple[str, str]] | None = None,
    title: str = "송장조회 결과",
) -> Path:
    """조회 결과 목록을 엑셀로 저장하고 저장한 경로를 돌려준다.

    applied_label: 성공 건의 '샵마인 반영' 칸에 넣을 문구. 반영까지 갔는지는
    주문 단위로 알 수 없고(샵마인 결과 창은 건수만 준다) 실행 단위로만 알 수
    있어서, 실행 결과를 그대로 성공 건에 붙인다.
    summary: 요약 시트의 '실행 정보'에 넣을 (항목, 값) 줄들.
    """
    path = Path(path)
    wb = Workbook()
    _write_entries_sheet(wb.active, entries, applied_label, title)
    _write_summary_sheet(wb.create_sheet(SUMMARY_SHEET_NAME), entries, summary or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _write_entries_sheet(ws, entries: list[ReportEntry], applied_label: str,
                         title: str) -> None:
    ws.title = SHEET_NAME
    last_col = len(HEADERS)
    rows = sorted(entries, key=_sort_key)

    _write_title(ws, title, rows, last_col)

    ws.append(HEADERS)
    for cell in ws[_HEADER_ROW]:
        cell.font = Font(bold=True, color="202124")
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _CELL_BORDER
    ws.row_dimensions[_HEADER_ROW].height = 22

    for i, entry in enumerate(rows):
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
        # 결과가 바뀌는 자리(그룹 끝)에는 굵은 선을 그어 덩어리를 나눈다.
        next_label = result_label(rows[i + 1]) if i + 1 < len(rows) else None
        _style_row(ws[ws.max_row], label, group_end=next_label != label)

    for i, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = f"A{_HEADER_ROW + 1}"
    ws.auto_filter.ref = (f"A{_HEADER_ROW}:{get_column_letter(last_col)}"
                          f"{max(ws.max_row, _HEADER_ROW)}")


def _write_title(ws, title: str, rows: list[ReportEntry], last_col: int) -> None:
    """맨 위 두 줄: 제목(검은 띠)과 결과별 건수 한 줄."""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    head = ws.cell(row=1, column=1, value=f"{title}   전체 {len(rows)}건")
    head.font = Font(bold=True, size=14, color="FFFFFF")
    head.fill = _TITLE_FILL
    head.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    counts = "     ".join(f"{label} {n}건" for label, n in label_counts(rows))
    line = ws.cell(row=2, column=1, value=counts)
    line.font = Font(bold=True, size=11, color="3C4043")
    line.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 22


def _style_row(row, label: str, *, group_end: bool) -> None:
    badge_fill, badge_font, row_fill = _COLORS.get(label, _DEFAULT_COLOR)
    fill = PatternFill("solid", fgColor=row_fill)
    border = Border(left=_THIN, right=_THIN, top=_THIN,
                    bottom=_GROUP_LINE if group_end else _THIN)
    for cell in row:
        cell.fill = fill
        cell.border = border
        cell.alignment = Alignment(vertical="center")

    badge = row[0]  # 결과
    badge.fill = PatternFill("solid", fgColor=badge_fill)
    badge.font = Font(bold=True, color=badge_font)
    badge.alignment = Alignment(horizontal="center", vertical="center")

    for cell in (row[1], row[4]):  # 마켓 주문번호, 송장번호
        cell.number_format = _TEXT_FORMAT
    row[5].alignment = Alignment(horizontal="center", vertical="center")  # 샵마인 반영
    row[6].alignment = Alignment(wrap_text=True, vertical="center")  # 사유


def _write_summary_sheet(ws, entries: list[ReportEntry],
                         summary: list[tuple[str, str]]) -> None:
    """요약 시트: 결과별 건수와 할 일을 먼저, 실행 정보/파일 경로를 그 아래에."""
    _write_summary_band(ws, 1, "결과별 건수")
    for col, name in enumerate(("결과", "건수", "해야 할 일"), start=1):
        cell = ws.cell(row=2, column=col, value=name)
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _CELL_BORDER

    for label, n in label_counts(entries):
        ws.append([label, n, _ACTIONS.get(label, "")])
        row = ws[ws.max_row]
        badge_fill, badge_font, row_fill = _COLORS.get(label, _DEFAULT_COLOR)
        for cell in row[:3]:
            cell.fill = PatternFill("solid", fgColor=row_fill)
            cell.border = _CELL_BORDER
            cell.alignment = Alignment(vertical="center")
        row[0].fill = PatternFill("solid", fgColor=badge_fill)
        row[0].font = Font(bold=True, color=badge_font)
        row[0].alignment = Alignment(horizontal="center", vertical="center")
        row[1].font = Font(bold=True)
        row[1].alignment = Alignment(horizontal="center", vertical="center")

    if summary:
        _write_summary_band(ws, ws.max_row + 2, "실행 정보")
        for name, value in summary:
            ws.append([name, value])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
            ws.merge_cells(start_row=ws.max_row, start_column=2,
                           end_row=ws.max_row, end_column=3)
            ws.cell(row=ws.max_row, column=2).alignment = Alignment(
                wrap_text=True, vertical="center")

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 78


def _write_summary_band(ws, row: int, text: str) -> None:
    """요약 시트의 구역 제목(검은 띠) 한 줄."""
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = Font(bold=True, size=13, color="FFFFFF")
    cell.fill = _TITLE_FILL
    cell.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[row].height = 26


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
    now = datetime.now()
    path = out_dir / f"송장조회결과_{now:%Y%m%d_%H%M%S}.xlsx"
    summary = [
        ("실행 시각", f"{now:%Y-%m-%d %H:%M:%S}"),
        ("조회 성공", f"{counts.get('success', 0)}건"),
        ("조회 실패", f"{counts.get('fail', 0)}건"),
        ("조회 스킵", f"{counts.get('skip', 0)}건"),
    ]
    if apply_note:
        summary.append(("샵마인 반영", apply_note))
    summary.extend((name, str(value)) for name, value in paths if value)
    try:
        saved = write_result_excel(entries, path, applied_label=applied_label,
                                   summary=summary,
                                   title=f"송장조회 결과  {now:%Y-%m-%d %H:%M}")
    except Exception as e:  # noqa: BLE001 - 결과 저장 실패가 실행 결과를 덮으면 안 된다
        log(f"경고: 결과 엑셀을 저장하지 못했습니다 - {e}")
        return None
    log(f"조회 결과 엑셀: {saved}")
    return saved
