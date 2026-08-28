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
from .suppliers.base import AdapterError, BlockedError, OrderCancelled, TrackingNotAvailableYet
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

    all_orders = excel_io.read_pending_orders(input_path)
    orders = all_orders[:limit] if limit is not None else all_orders
    # 중복 주문 판정은 --limit 로 잘라낸 목록이 아니라 엑셀 전체를 기준으로
    # 해야 한다 - 잘린 쪽 행도 샵마인 그리드에는 그대로 남아 있다.
    total = len(orders)

    upload_rows: list[tuple[str, str, str | None]] = []

    with sync_playwright() as p:
        supplier_contexts: dict[str, tuple] = {}
        # 로그인 자체가 막힌 사이트는 주문마다 몇 분씩 다시 로그인 대기를
        # 반복하지 않도록, 한 번 막히면 이후 주문은 바로 스킵한다.
        blocked_sites: dict[str, str] = {}

        for i, order in enumerate(orders, start=1):
            message = ""
            try:
                adapter = get_adapter(order.product_url)
                if adapter is None:
                    message = f"등록된 어댑터 없음: {order.product_url}"
                    report.unsupported_site(order.order_id, order.product_url,
                                            recipient_name=order.recipient_name)
                    continue

                site_key = adapter.SITE_KEY
                if site_key in blocked_sites:
                    message = f"건너뜀: {blocked_sites[site_key]}"
                    report.skip(order.order_id, message)
                    continue

                if site_key not in supplier_contexts:
                    supplier_contexts[site_key] = browser_mod.get_context(p, site_key, headless=headless)
                _, supplier_context = supplier_contexts[site_key]

                # 옥션처럼 상품URL만으로 주문을 특정할 수 없는 공급사는 수령인
                # 이름까지 봐야 어느 주문인지 확정할 수 있다. 그런 어댑터만
                # WANTS_RECIPIENT_NAME으로 표시해두고, 나머지 어댑터의
                # 시그니처는 그대로 둔다.
                extra_kwargs = {}
                if getattr(adapter, "WANTS_RECIPIENT_NAME", False):
                    extra_kwargs["recipient_name"] = order.recipient_name

                try:
                    result = adapter.get_tracking(
                        supplier_context,
                        order.product_url,
                        headless=headless,
                        order_option=order.order_option,
                        **extra_kwargs,
                    )
                except TrackingNotAvailableYet as e:
                    message = "아직 송장번호 미발급 - 건너뜀"
                    # 어댑터가 주문상세에서 읽어 예외에 실어준 주문일을 같이
                    # 남긴다 - 미발급인 채로 며칠 지난 주문은 따로 모아야 한다.
                    report.skip(order.order_id, "아직 송장번호 미발급",
                                recipient_name=order.recipient_name,
                                order_date=e.order_date)
                    continue
                except OrderCancelled as e:
                    # 기다려도 송장이 안 나오는 주문이라 일반 스킵과 분리한다.
                    message = f"취소/품절로 보임 - 건너뜀 ({e})"
                    report.cancelled(order.order_id, str(e),
                                     recipient_name=order.recipient_name,
                                     order_date=e.order_date)
                    continue
                except BlockedError as e:
                    blocked_sites[site_key] = str(e)
                    raise

                upload_rows.append((order.order_id, result.tracking_no, result.courier))
                report.success(order.order_id, result.courier, result.tracking_no,
                               order_date=result.order_date)
                message = f"성공 ({result.courier} / {result.tracking_no})"

            except AdapterError as e:
                message = f"실패: {e}"
                if order.recipient_name:
                    message += f" (수령인: {order.recipient_name})"
                report.fail(order.order_id, str(e), recipient_name=order.recipient_name,
                            order_date=e.order_date)
            except Exception as e:  # noqa: BLE001 - 배치 전체가 죽지 않도록 광범위하게 잡는다
                reason = f"예상치 못한 오류: {e}"
                message = reason
                if order.recipient_name:
                    message += f" (수령인: {order.recipient_name})"
                # reason에는 수령인을 넣지 않는다 - recipient_name 필드가 따로
                # 있어서 마지막 실패 목록에서 이름이 두 번 찍히게 된다.
                report.fail(order.order_id, reason, recipient_name=order.recipient_name)
            finally:
                if on_progress is not None:
                    on_progress(i, total, order.order_id, message)

            rate_limit.humanized_delay(settings.delay_min, settings.delay_max)

        for site_key, (b, c) in supplier_contexts.items():
            browser_mod.save_state(c, site_key)
            b.close()

    upload_rows = _drop_split_orders(all_orders, upload_rows, report)

    if upload_rows:
        excel_io.write_upload_file(upload_rows, output_path)

    return report


def _drop_split_orders(orders, upload_rows, report):
    """업로드 대상에서 위험한 중복 주문을 빼고, 뺀 건은 리포트에 남긴다.

    판단 기준과 이유는 excel_io.resolve_duplicate_orders 참고.
    """
    kept, dropped = excel_io.resolve_duplicate_orders(orders, upload_rows)
    if dropped:
        recipients = {o.order_id: o.recipient_name for o in orders}
        for order_id in dropped:
            report.exclude(order_id, excel_io.SPLIT_ORDER_REASON,
                           recipient_name=recipients.get(order_id))
    return kept
