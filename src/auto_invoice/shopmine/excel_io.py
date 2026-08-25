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

    orders: list[PendingOrder] = []
    for row in data_rows:
        if row is None or len(row) <= max(id_idx, url_idx):
            continue
        raw_id = row[id_idx]
        raw_url = row[url_idx]
        if not raw_id or not raw_url:
            continue
        orders.append(PendingOrder(order_id=_clean_id(raw_id), product_url=str(raw_url).strip()))

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
