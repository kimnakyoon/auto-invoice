"""전체 배치 흐름.

샵마인 '발송대상' 엑셀(입력) -> 공급사별 송장번호 조회 -> 샵마인
'발송정보일괄등록(수정용)' 형식 엑셀(출력) 생성까지만 담당한다.
생성된 엑셀을 실제로 업로드하는 것은 사람이 직접 한다.

조회는 공급사별로 나눠 동시에 돈다. 사이트끼리는 서로 아무 상관이 없는데
한 줄로 세워 돌리면 전체 시간이 '모든 사이트의 합'이 되고, 한 건에 수십
초씩이라 그게 실행 시간의 거의 전부다. 같은 사이트 안에서는 예전처럼 한
건씩 순서대로 본다 - 한 사이트에 요청을 몰아치면 봇으로 보인다.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from . import rate_limit
from .config import load_settings
from .report import RunReport
from .shopmine import excel_io
from .suppliers.base import AdapterError, BlockedError, OrderCancelled, TrackingNotAvailableYet
from .suppliers.registry import get_adapter

ProgressCallback = Callable[[int, int, str, str], None]
# 주문 한 건이 끝날 때마다 (지금까지의 결과, 지금까지의 업로드 행)을 넘긴다.
# 실행이 중간에 멈춰도 그 지점부터 다시 시작할 수 있게 하는 데 쓴다
# (checkpoint.py / pipeline.py).
CheckpointCallback = Callable[[list, list], None]


class _Shared:
    """공급사 스레드들이 같이 쓰는 결과 모음.

    RunReport에 남기는 것과 업로드 행 추가는 전부 이 락 안에서 한다. 진행
    표시와 진행 상황 저장은 락 밖에서 부른다 - 파일 쓰기나 GUI 큐에 넣는
    일이라, 락을 쥔 채로 하면 다른 공급사가 그동안 멈춘다.
    """

    def __init__(self, report: RunReport, upload_rows: list, total: int, done: int,
                 on_progress: ProgressCallback | None,
                 on_checkpoint: CheckpointCallback | None) -> None:
        self.lock = threading.Lock()
        self.report = report
        self.upload_rows = upload_rows
        self.total = total
        self._done = done
        self._on_progress = on_progress
        self._on_checkpoint = on_checkpoint

    def finished(self, order_id: str, message: str) -> None:
        """주문 한 건을 끝냈다고 알린다 (성공/실패/스킵 어느 쪽이든)."""
        with self.lock:
            self._done += 1
            done = self._done
            entries = list(self.report.entries)
            rows = list(self.upload_rows)
        if self._on_progress is not None:
            self._on_progress(done, self.total, order_id, message)
        # 여기서 저장해두면 다음 주문을 조회하다 멈춰도 지금까지의 결과는 남는다.
        if self._on_checkpoint is not None:
            self._on_checkpoint(entries, rows)


def run(
    input_path: str,
    output_path: str,
    limit: int | None = None,
    headless: bool = True,
    on_progress: ProgressCallback | None = None,
    done_entries: Iterable | None = None,
    done_rows: Iterable | None = None,
    on_checkpoint: CheckpointCallback | None = None,
) -> RunReport:
    """on_progress(끝낸 건수, 전체, order_id, message) — GUI 등에서 진행 상황을 보여줄 때 사용.

    공급사를 동시에 돌리므로 '몇 번째 주문'이 아니라 '지금까지 끝낸 건수'를
    넘긴다 - 엑셀 순서대로 끝나지 않는다.

    done_entries / done_rows: 지난 실행에서 이미 조회를 끝낸 결과. 여기 있는
    주문은 공급사에 다시 묻지 않고 그 결과를 그대로 쓴다 - 실행이 중간에
    멈췄을 때 '멈춘 지점부터' 이어서 하기 위한 것이다. 한 건에 수십 초씩
    걸리므로 이미 끝낸 건을 다시 조회하는 것이 가장 큰 낭비다.

    on_checkpoint: 주문 한 건이 끝날 때마다 부른다 (진행 상황 저장용).
    """
    settings = load_settings()
    report = RunReport()
    report.entries.extend(done_entries or [])

    all_orders = excel_io.read_pending_orders(input_path)
    orders = all_orders[:limit] if limit is not None else all_orders
    # 중복 주문 판정은 --limit 로 잘라낸 목록이 아니라 엑셀 전체를 기준으로
    # 해야 한다 - 잘린 쪽 행도 샵마인 그리드에는 그대로 남아 있다.
    total = len(orders)

    # 이미 결과가 있는 주문은 건너뛴다. 판단 기준을 '업로드 행'이 아니라
    # '결과가 있는가'로 두는 이유는, 조회에 실패하거나 스킵한 건도 다시
    # 물어볼 필요가 없기 때문이다 (실패 사유는 대개 다시 해도 같다).
    #
    # 개수로 세는 이유: 한 주문번호가 엑셀에 여러 행으로 있는 주문(상품별 행)이
    # 있어서, 주문번호 집합으로 판단하면 첫 행만 끝냈는데 나머지 행까지
    # '이미 했다'고 건너뛰게 된다.
    remaining_done = Counter(e.order_id for e in report.entries)
    upload_rows: list[tuple[str, str, str | None]] = [tuple(r) for r in (done_rows or [])]

    pending = []
    for order in orders:
        if remaining_done.get(order.order_id):
            remaining_done[order.order_id] -= 1
            continue
        pending.append(order)

    shared = _Shared(report, upload_rows, total, total - len(pending),
                     on_progress, on_checkpoint)

    # 공급사별로 묶는다. 어댑터가 없는 주문은 브라우저를 열 필요가 없으니
    # 여기서 끝낸다.
    by_site: dict[str, list] = {}
    for order in pending:
        adapter = get_adapter(order.product_url)
        if adapter is None:
            with shared.lock:
                report.unsupported_site(order.order_id, order.product_url,
                                        recipient_name=order.recipient_name)
            shared.finished(order.order_id, f"등록된 어댑터 없음: {order.product_url}")
            continue
        by_site.setdefault(adapter.SITE_KEY, []).append((order, adapter))

    if by_site:
        workers = max(1, min(settings.workers, len(by_site)))
        # 주문이 많은 사이트부터 맡긴다. 전체 시간은 '가장 오래 걸리는 사이트가
        # 언제 끝나는가'로 정해지는데, 지금까지는 엑셀에 먼저 나온 사이트부터
        # 맡겨서 제일 큰 사이트가 뒤로 밀리면 그만큼 통째로 늦어졌다. 실제로
        # 2026-09-01 실행은 98건 중 51건이 롯데온이었다 - 이 한 사이트가 언제
        # 시작하느냐가 전체 시간이다. 작은 사이트들은 남는 일꾼이 알아서 채운다.
        by_size = sorted(by_site.items(), key=lambda item: len(item[1]), reverse=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_lookup_site, site_key, jobs,
                                   settings=settings, headless=headless, shared=shared)
                       for site_key, jobs in by_size]
            for future in futures:
                future.result()  # 스레드 안에서 터진 예외를 여기서 다시 올린다

    # 스레드가 끝난 순서대로 쌓였으므로, 사람이 보는 순서(엑셀 순서)로 되돌린다.
    _restore_order(orders, report, upload_rows)

    kept = _drop_split_orders(all_orders, upload_rows, report)

    if kept:
        excel_io.write_upload_file(kept, output_path)

    return report


def _lookup_site(site_key: str, jobs: list, *, settings, headless: bool,
                 shared: _Shared) -> None:
    """한 공급사의 주문들을 순서대로 조회한다 (스레드 하나가 이 함수를 맡는다).

    Playwright sync API 객체는 만든 스레드 밖에서 쓸 수 없어서, 스레드마다
    자기 Playwright를 연다.
    """
    with sync_playwright() as p, contextlib.ExitStack() as stack:
        if getattr(jobs[0][1], "WANTS_CDP_CHROME", False):
            # 번들 크로미엄이라는 것 자체로 봇 확인에 걸리는 사이트(옥션)는
            # 우리가 직접 실행한 진짜 크롬(CDP)에서 조회한다 - 창이 하나 뜬다.
            # (어댑터의 WANTS_CDP_CHROME 주석 참고. 프로필이 남아 로그인도
            # 유지되므로 headless 설정과 무관하게 이 경로를 쓴다.)
            browser_mod.remember_playwright(p)
            context = stack.enter_context(
                browser_mod.real_chrome_cdp_context(site_key, p))
        else:
            browser, context = browser_mod.get_context(p, site_key, headless=headless)
            stack.callback(browser.close)
        # 로그인 자체가 막힌 사이트는 주문마다 몇 분씩 다시 로그인 대기를
        # 반복하지 않도록, 한 번 막히면 이후 주문은 바로 스킵한다.
        blocked_reason: str | None = None
        try:
            # 주문목록 화면 한 번으로 '아직 안 나간 주문'을 미리 걸러낼 수 있는
            # 어댑터는 여기서 그 기회를 준다 (롯데온 prepare_batch). 주문마다
            # 상세를 여는 것보다 훨씬 싸다. 실패하면 어댑터가 아무것도 읽지 않은
            # 것과 같아서, 모든 주문이 예전처럼 상세를 여는 경로로 간다.
            prepare = getattr(jobs[0][1], "prepare_batch", None)
            if prepare is not None:
                with contextlib.suppress(Exception):
                    prepare(context, [order for order, _ in jobs], headless=headless)

            # 요청 간격: 봇으로 보이지 않게 같은 사이트에는 1.5~4초(설정값)
            # 간격으로만 요청한다. 예전에는 성공/실패한 주문 '뒤에' 이 시간을
            # 통째로 자서 실제 간격이 '조회 시간 + 대기'로 늘 길었고, 반대로
            # 요청을 보내고도 미발급이라 스킵한 주문 뒤에는 간격이 아예 없었다.
            # 지금은 '요청을 시작한 시각 + 랜덤 간격' 전에는 다음 요청을
            # 시작하지 않는다 - 조회에 걸린 시간이 간격에 포함되고, 요청을
            # 보냈으면 결과가 무엇이든 간격이 지켜진다 (2026-09-01 결정).
            # 요청을 안 보낸 주문(롯데온이 목록으로 답한 것)은 간격을 새로
            # 세우지 않고, 대기가 요청 '앞'에 붙으므로 마지막 주문 뒤에는
            # 자연히 아무것도 기다리지 않는다.
            gate = 0.0  # 다음 요청을 시작해도 되는 시각 (time.monotonic 기준)
            for order, adapter in jobs:
                message = ""
                try:
                    if blocked_reason:
                        # 봇 확인/로그인 차단으로 조회 자체를 못 한 주문은 스킵이
                        # 아니라 **실패**로 남긴다 - 마지막 결과의 실패 목록에
                        # 사유(봇 확인 등)와 함께 보여서 사람이 직접 처리하게
                        # (2026-09-01 사용자 요청). '미발급 스킵'과 섞이면
                        # 기다리면 되는 주문처럼 보여 놓치게 된다.
                        message = f"실패: {blocked_reason}"
                        with shared.lock:
                            shared.report.fail(order.order_id, blocked_reason,
                                               recipient_name=order.recipient_name,
                                               product_url=order.product_url)
                        continue

                    # 옥션처럼 상품URL만으로 주문을 특정할 수 없는 공급사는 수령인
                    # 이름까지 봐야 어느 주문인지 확정할 수 있다. 그런 어댑터만
                    # WANTS_RECIPIENT_NAME으로 표시해두고, 나머지 어댑터의
                    # 시그니처는 그대로 둔다.
                    extra_kwargs = {}
                    if getattr(adapter, "WANTS_RECIPIENT_NAME", False):
                        extra_kwargs["recipient_name"] = order.recipient_name

                    # 지난 요청과의 간격이 아직 안 찼으면 모자란 만큼만 쉰다.
                    remaining = gate - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)

                    lookup_started = time.monotonic()
                    sent_request = True  # 무슨 일이 있었는지 모르면 보냈다고 본다
                    try:
                        try:
                            result = adapter.get_tracking(
                                context,
                                order.product_url,
                                headless=headless,
                                order_option=order.order_option,
                                **extra_kwargs,
                            )
                        except AdapterError as e:
                            sent_request = e.sent_request
                            raise
                        finally:
                            if sent_request:
                                # 봇 확인이 잘 뜨는 사이트(옥션/지마켓)는 어댑터가
                                # 자기 간격(REQUEST_GAP)을 따로 정한다 - 기본
                                # 간격(1.5~4초)은 조회 자체에 걸리는 시간보다
                                # 짧아서, 사실상 쉬지 않고 다음 주문을 열었다.
                                gap_min, gap_max = getattr(
                                    adapter, "REQUEST_GAP",
                                    (settings.delay_min, settings.delay_max))
                                gate = lookup_started + rate_limit.request_gap(
                                    gap_min, gap_max)
                    except TrackingNotAvailableYet as e:
                        # 사유까지 로그에 남긴다 - '취소' 표시가 있는데 '준비'가
                        # 함께 있어 취소 대신 미발급으로 넘긴 건이 여기 섞인다.
                        detail = f" ({e})" if str(e) else ""
                        message = f"아직 송장번호 미발급 - 건너뜀{detail}"
                        # 어댑터가 주문상세에서 읽어 예외에 실어준 주문일을 같이
                        # 남긴다 - 미발급인 채로 며칠 지난 주문은 따로 모아야 한다.
                        with shared.lock:
                            shared.report.skip(order.order_id, "아직 송장번호 미발급",
                                               recipient_name=order.recipient_name,
                                               order_date=e.order_date,
                                               delivery_note=e.delivery_note,
                                               product_url=order.product_url)
                        continue
                    except OrderCancelled as e:
                        # 기다려도 송장이 안 나오는 주문이라 일반 스킵과 분리한다.
                        message = f"취소/품절로 보임 - 건너뜀 ({e})"
                        with shared.lock:
                            shared.report.cancelled(order.order_id, str(e),
                                                    recipient_name=order.recipient_name,
                                                    order_date=e.order_date,
                                                    delivery_note=e.delivery_note,
                                                    product_url=order.product_url)
                        continue
                    except BlockedError as e:
                        blocked_reason = str(e)
                        raise

                    with shared.lock:
                        shared.upload_rows.append(
                            (order.order_id, result.tracking_no, result.courier))
                        shared.report.success(order.order_id, result.courier, result.tracking_no,
                                              order_date=result.order_date,
                                              delivery_note=result.delivery_note,
                                              product_url=order.product_url)
                    message = f"성공 ({result.courier} / {result.tracking_no})"

                except AdapterError as e:
                    message = f"실패: {e}"
                    if order.recipient_name:
                        message += f" (수령인: {order.recipient_name})"
                    with shared.lock:
                        shared.report.fail(order.order_id, str(e),
                                           recipient_name=order.recipient_name,
                                           order_date=e.order_date,
                                           delivery_note=e.delivery_note,
                                           product_url=order.product_url)
                except Exception as e:  # noqa: BLE001 - 배치 전체가 죽지 않도록 광범위하게 잡는다
                    reason = f"예상치 못한 오류: {e}"
                    message = reason
                    if order.recipient_name:
                        message += f" (수령인: {order.recipient_name})"
                    # reason에는 수령인을 넣지 않는다 - recipient_name 필드가 따로
                    # 있어서 마지막 실패 목록에서 이름이 두 번 찍히게 된다.
                    with shared.lock:
                        shared.report.fail(order.order_id, reason,
                                           recipient_name=order.recipient_name,
                                           product_url=order.product_url)
                finally:
                    shared.finished(order.order_id, message)
        finally:
            # CDP 크롬은 프로필 자체가 남아 세션 저장이 필수는 아니고,
            # 실패해도 조회 결과를 잃으면 안 되므로 조용히 넘어간다.
            # (브라우저 정리는 ExitStack이 한다.)
            with contextlib.suppress(Exception):
                browser_mod.save_state(context, site_key)


def _restore_order(orders, report: RunReport, upload_rows: list) -> None:
    """결과를 엑셀에 있던 주문 순서로 되돌린다 (제자리 정렬).

    공급사를 동시에 돌리면 끝나는 순서가 매번 달라진다. 그대로 두면 결과
    엑셀과 로그의 줄 순서가 실행할 때마다 바뀌어서, 사람이 지난 실행과
    비교하기 어렵다. 같은 주문번호끼리의 순서는 정렬이 안정적이라 유지된다.
    """
    rank: dict[str, int] = {}
    for i, order in enumerate(orders):
        rank.setdefault(order.order_id, i)
    last = len(orders)
    report.entries.sort(key=lambda e: rank.get(e.order_id, last))
    upload_rows.sort(key=lambda row: rank.get(row[0], last))


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
