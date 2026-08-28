"""샵마인과의 연동은 화면 자동화가 아니라 엑셀/CSV 파일로 한다.

샵마인의 주문 상세/CS메모 화면은 UI Automation으로 조회하면 앱이 반복적으로
크래시하는 것을 실제로 확인했다 (커스텀 렌더링 DataGridView 추정). 반면
샵마인은 다음 두 기능을 안전하게 제공한다:

  1. 주문관리 > 발송대상 > [엑셀파일생성] : 송장번호가 필요한 주문 목록을
     엑셀(.xls, 구버전 BIFF 포맷)로 내보내기. 실제로 받아본 파일의 헤더는
     "수령인", "마켓 주문번호", "상품URL" 이다.
  2. 발송정보일괄등록(수정용) : 아래 헤더의 엑셀(.xls) 또는 CSV를 업로드해
     일괄 반영. 샵마인이 제공한 샘플 파일 기준 헤더는 "주문고유코드",
     "송장번호", "택배사" — 식별 컬럼명은 "주문고유코드/고객주문번호/주문번호/
     출고번호/원장주문코드" 중 아무거나 인식하지만, 내보내기 파일의
     "마켓 주문번호" 값과 의미가 가장 정확히 대응하는 건 "고객주문번호"라
     그 헤더를 쓴다. 업로드는 xlsx를 지원하지 않는다고 안내되어 있어(xls,
     csv만 지원) CSV(UTF-8 BOM, 엑셀 호환)로 생성한다.

그래서 이 모듈은 (1)에서 받은 엑셀을 읽고, (2)에 맞는 CSV를 생성하는
역할만 한다. 업로드 자체(파일 선택 -> 일괄등록 클릭)는 사람이 직접 한다 -
이 마지막 확인 단계를 사람이 갖는 것 자체가 안전장치이기도 하다.
"""

from __future__ import annotations

import csv
from pathlib import Path

import openpyxl
import xlrd

from ..models import PendingOrder

# 샵마인 "발송대상 > 엑셀파일생성" 내보내기 파일의 컬럼명
EXPORT_ORDER_ID_HEADER = "마켓 주문번호"
EXPORT_PRODUCT_URL_HEADER = "상품URL"
# 필수는 아니고, 실패한 주문을 사람이 샵마인에서 찾기 쉽게 리포트에 남기는
# 용도로만 쓴다 - 없어도 동작에는 지장 없다.
EXPORT_RECIPIENT_NAME_HEADER = "수령인"
# 필수는 아니고(샵마인 기본 내보내기 항목이 아니라 사용자가 직접 추가한
# 컬럼), 한 공급사 주문에 상품별로 송장번호가 여러 개 있을 때 이 주문이
# 어느 상품(색상/사이즈 등)인지 구분해서 맞는 송장을 고르는 데 쓴다.
EXPORT_ORDER_OPTION_HEADER = "주문옵션"

# 샵마인 "발송정보일괄등록(수정용)" 업로드 파일이 요구하는 컬럼명
UPLOAD_ORDER_ID_HEADER = "고객주문번호"
UPLOAD_TRACKING_HEADER = "송장번호"
UPLOAD_COURIER_HEADER = "택배사"


def _clean_id(value) -> str:
    """엑셀 숫자 셀이 float(예: 21102492359043.0)로 읽히는 경우를 정리한다."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_rows(path: str) -> tuple[list[str], list[list]]:
    """확장자에 따라 .xls(xlrd)/.xlsx(openpyxl)를 모두 지원한다."""
    ext = Path(path).suffix.lower()

    if ext == ".xls":
        wb = xlrd.open_workbook(path)
        sheet = wb.sheet_by_index(0)
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
    elif ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, data_only=True)
        sheet = wb.active
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    else:
        raise ValueError(f"지원하지 않는 파일 형식입니다: {ext} (xls, xlsx만 가능)")

    if not rows:
        raise ValueError(f"엑셀 파일에 데이터가 없습니다: {path}")

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    return headers, rows[1:]


def read_pending_orders(path: str) -> list[PendingOrder]:
    headers, data_rows = _read_rows(path)

    try:
        id_idx = headers.index(EXPORT_ORDER_ID_HEADER)
        url_idx = headers.index(EXPORT_PRODUCT_URL_HEADER)
    except ValueError as e:
        raise ValueError(
            f"엑셀에서 필요한 컬럼('{EXPORT_ORDER_ID_HEADER}', '{EXPORT_PRODUCT_URL_HEADER}')을 "
            f"찾을 수 없습니다. 실제 헤더: {headers}"
        ) from e
    # 없어도 동작에는 지장 없는 선택 컬럼들이라 못 찾아도 에러 내지 않는다.
    recipient_idx = headers.index(EXPORT_RECIPIENT_NAME_HEADER) if EXPORT_RECIPIENT_NAME_HEADER in headers else None
    option_idx = headers.index(EXPORT_ORDER_OPTION_HEADER) if EXPORT_ORDER_OPTION_HEADER in headers else None

    def _optional_cell(row: list, idx: int | None) -> str:
        if idx is None or len(row) <= idx or not row[idx]:
            return ""
        return str(row[idx]).strip()

    orders: list[PendingOrder] = []
    for row in data_rows:
        if row is None or len(row) <= max(id_idx, url_idx):
            continue
        raw_id = row[id_idx]
        raw_url = row[url_idx]
        if not raw_id or not raw_url:
            continue
        orders.append(
            PendingOrder(
                order_id=_clean_id(raw_id),
                product_url=str(raw_url).strip(),
                recipient_name=_optional_cell(row, recipient_idx),
                order_option=_optional_cell(row, option_idx),
            )
        )

    return orders


def write_upload_file(rows: list[tuple[str, str, str | None]], path: str) -> None:
    """rows: (고객주문번호, 송장번호, 택배사) 튜플 목록.

    샵마인 업로드가 xlsx를 지원하지 않는다고 안내되어 있어 CSV로 만든다.
    Excel에서 한글이 깨지지 않도록 UTF-8 BOM(utf-8-sig)으로 인코딩한다.
    """
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([UPLOAD_ORDER_ID_HEADER, UPLOAD_TRACKING_HEADER, UPLOAD_COURIER_HEADER])
        for order_id, tracking_no, courier in rows:
            writer.writerow([order_id, tracking_no, courier or ""])


def append_upload_rows(rows: list[tuple[str, str, str | None]], path: str) -> None:
    """이미 write_upload_file로 만들어진 업로드 파일에 rows를 추가한다.

    파일이 아직 없으면(예: 다른 공급사 성공 건이 0건이라 write_upload_file이
    호출되지 않은 경우) 헤더부터 새로 만든다 - CJ온스타일처럼 별도 경로로
    조회한 결과를 같은 업로드 파일에 합칠 때 쓴다.
    """
    if not rows:
        return
    file_exists = Path(path).exists()
    with open(path, "a" if file_exists else "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([UPLOAD_ORDER_ID_HEADER, UPLOAD_TRACKING_HEADER, UPLOAD_COURIER_HEADER])
        for order_id, tracking_no, courier in rows:
            writer.writerow([order_id, tracking_no, courier or ""])


# resolve_duplicate_orders가 뺀 주문을 리포트에 남길 때 쓰는 사유.
SPLIT_ORDER_REASON = (
    "한 주문번호가 여러 행으로 나뉜 주문인데 일부 행만 송장이 확인됐거나 "
    "행마다 송장번호가 달랐습니다. 일괄등록은 주문번호로만 행을 찾아서 "
    "나머지 행에도 같은 송장이 들어가므로 자동 반영에서 제외했습니다 - "
    "샵마인에서 직접 처리해주세요."
)


def _order_row_counts(orders: list[PendingOrder]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for order in orders:
        counts[order.order_id] = counts.get(order.order_id, 0) + 1
    return counts


def count_export_rows(export_path: str, order_ids) -> int:
    """order_ids에 해당하는 주문이 내보내기 엑셀에서 차지하는 '행' 수.

    샵마인 그리드에서 실제로 채워질 행 수와 같다 (아래 resolve_duplicate_orders
    참고). CSV 줄 수와 다를 수 있어서, 반영 건수 검증은 이 값을 기준으로 한다.
    """
    counts = _order_row_counts(read_pending_orders(export_path))
    return sum(counts.get(order_id, 1) for order_id in order_ids)


def resolve_duplicate_orders(
    orders: list[PendingOrder],
    upload_rows: list[tuple[str, str, str | None]],
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    """한 주문번호가 여러 행으로 나뉜 주문을 업로드 대상에서 안전하게 정리한다.

    샵마인 일괄등록은 '고객주문번호'로만 행을 찾기 때문에, 한 주문번호가
    그리드에 여러 행(상품별 행)으로 있으면 CSV 한 줄이 그 행 **전부**를 같은
    송장번호로 채운다. 실제로 2026-08-28 실행에서 주문 22102471952623이 두
    행이었고 한 행만 송장이 나왔는데, 그대로 올렸다면 아직 발송되지 않은
    나머지 행에도 같은 송장번호가 들어갈 뻔했다 (6단계 건수 검증에 걸려 멈춤).

    그래서 규칙을 하나로 둔다: 그 주문의 **모든 행이 같은 송장번호로 조회
    성공**했을 때만 CSV 한 줄로 합쳐서 올리고, 하나라도 빠지거나 송장번호가
    서로 다르면 그 주문은 통째로 뺀다. 뺀 주문은 사람이 직접 처리한다.

    반환: (정리된 upload_rows, 제외한 주문번호 목록)
    """
    counts = _order_row_counts(orders)
    by_id: dict[str, list[tuple[str, str, str | None]]] = {}
    for row in upload_rows:
        by_id.setdefault(row[0], []).append(row)

    kept: list[tuple[str, str, str | None]] = []
    dropped: list[str] = []
    for order_id, rows in by_id.items():
        expected = counts.get(order_id, 1)
        if len(rows) == expected and len({r[1] for r in rows}) == 1:
            kept.append(rows[0])
        else:
            dropped.append(order_id)
    return kept, dropped
