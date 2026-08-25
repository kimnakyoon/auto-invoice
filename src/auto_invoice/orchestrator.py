"""전체 배치 흐름.

샵마인 '발송대상' 엑셀(입력) -> 공급사별 송장번호 조회 -> 샵마인
'발송정보일괄등록(수정용)' 형식 엑셀(출력) 생성까지만 담당한다.
생성된 엑셀을 실제로 업로드하는 것은 사람이 직접 한다.
"""

from __future__ import annotations

from typing import Callable

from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from . import rate_limit
from .config import load_settings
from .report import RunReport
from .shopmine import excel_io
from .suppliers.base import AdapterError, TrackingNotAvailableYet
from .suppliers.registry import get_adapter

ProgressCallback = Callable[[int, int, str, str], None]


def run(
    input_path: str,
    output_path: str,
    limit: int | None = None,
    headless: bool = True,
    on_progress: ProgressCallback | None = None,
) -> RunReport:
    """on_progress(index, total, order_id, message) — GUI 등에서 진행 상황을 보여줄 때 사용."""
    settings = load_settings()
    report = RunReport()

    orders = excel_io.read_pending_orders(input_path)
    if limit is not None:
        orders = orders[:limit]
    total = len(orders)

    upload_rows: list[tuple[str, str, str | None]] = []

    with sync_playwright() as p:
        supplier_contexts: dict[str, tuple] = {}

        for i, order in enumerate(orders, start=1):
            message = ""
            try:
                adapter = get_adapter(order.product_url)
                if adapter is None:
                    message = f"등록된 어댑터 없음: {order.product_url}"
                    report.skip(order.order_id, message)
                    continue

                site_key = adapter.SITE_KEY
                if site_key not in supplier_contexts:
                    supplier_contexts[site_key] = browser_mod.get_context(p, site_key, headless=headless)
                _, supplier_context = supplier_contexts[site_key]

                try:
                    result = adapter.get_tracking(supplier_context, order.product_url, headless=headless)
                except TrackingNotAvailableYet:
                    message = "아직 송장번호 미발급 - 건너뜀"
                    report.skip(order.order_id, "아직 송장번호 미발급")
                    continue

                upload_rows.append((order.order_id, result.tracking_no, result.courier))
                report.success(order.order_id, result.courier, result.tracking_no)
                message = f"성공 ({result.courier} / {result.tracking_no})"

            except AdapterError as e:
                message = f"실패: {e}"
                report.fail(order.order_id, str(e))
            except Exception as e:  # noqa: BLE001 - 배치 전체가 죽지 않도록 광범위하게 잡는다
                message = f"예상치 못한 오류: {e}"
                report.fail(order.order_id, message)
            finally:
                if on_progress is not None:
                    on_progress(i, total, order.order_id, message)

            rate_limit.humanized_delay(settings.delay_min, settings.delay_max)

        for site_key, (b, c) in supplier_contexts.items():
            browser_mod.save_state(c, site_key)
            b.close()

    if upload_rows:
        excel_io.write_upload_file(upload_rows, output_path)

    return report
