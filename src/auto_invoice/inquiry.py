"""주문일이 이틀 지나도록 안 나간 주문에 공급사 사이트로 1:1 문의를 남긴다.

왜 필요한가: 송장조회 결과 엑셀의 '주문일지연' 시트에 '2일 지남' 건이 쌓이면
사람이 그 주문의 공급사 화면을 하나씩 열어 "○○○ 배송 언제 시작하나요?"를
남겨왔다. 그 일을 GUI의 [문의] 버튼 하나로 대신한다.

무엇을 문의하나 - 딱 '2일 지남'으로 기록된 건만이다 (사용자 기준):
  - 3일 이상 지난 건은 하지 않는다. '2일 지남'이던 날에 이미 문의를 남겼을
    테니 또 남기면 같은 주문에 문의가 두 번 쌓인다.
  - '출고예정/도착예정'이 적혀 있어도 2일 지남이면 남긴다 - 예정일이 있다고
    실제로 나가는 건 아니어서다.
  - 기준은 조회를 돌린 날의 결과 엑셀에 적힌 '지난 일수'다. 오늘 다시 계산하지
    않는다 - 엑셀에 '2일'이라고 적힌 그 건이 문의 대상이다.

같은 주문에 두 번 남기지 않도록 logs/inquiries.json 에 남긴 문의를 적어둔다.
버튼을 하루에 두 번 누르거나, 같은 엑셀로 다시 돌려도 이미 남긴 주문은 건너뛴다.

사이트마다 문의 화면이 달라서 어댑터에 post_inquiry(context, product_url,
recipient_name, headless)가 있는 사이트만 처리하고(지금은 롯데온), 없는
사이트는 '아직 지원 안 함'으로 결과에 남긴다 - 사람이 그 건은 직접 남긴다.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from . import rate_limit
from .config import load_settings
from .report import LOG_DIR
from .result_excel import DEFAULT_DIR as RESULT_DIR
from .result_excel import STALE_HEADERS, STALE_SHEET_NAME
from .suppliers import common
from .suppliers.base import AdapterError, BlockedError
from .suppliers.registry import get_adapter

# 결과 엑셀 '주문일지연' 시트의 '지난 일수' 칸이 이 값인 건만 문의한다.
TARGET_DAYS_TEXT = "2일"

# 남긴 문의 장부. 같은 마켓 주문번호에 두 번 남기지 않는 근거다.
LEDGER_PATH = LOG_DIR / "inquiries.json"

# 결과 엑셀 파일 이름 (result_excel.save_run_result와 같은 규칙).
RESULT_GLOB = "송장조회결과_*.xlsx"

LogFn = Callable[[str], None]


@dataclass
class InquiryTarget:
    """엑셀 '주문일지연' 시트의 '2일 지남' 한 줄."""

    order_id: str          # 마켓 주문번호 (샵마인 기준)
    recipient_name: str
    product_url: str
    order_date: str = ""
    delivery_note: str = ""

    @property
    def site_key(self) -> str | None:
        adapter = get_adapter(self.product_url) if self.product_url else None
        return getattr(adapter, "SITE_KEY", None) if adapter else None


@dataclass
class InquiryResult:
    order_id: str
    recipient_name: str
    site_key: str
    status: str            # "success" | "fail" | "skip"
    reason: str = ""
    product_url: str = ""


@dataclass
class InquiryRun:
    excel_path: Path | None = None
    targets: list[InquiryTarget] = field(default_factory=list)
    results: list[InquiryResult] = field(default_factory=list)
    stopped_reason: str | None = None

    def counts(self) -> dict[str, int]:
        out = {"success": 0, "fail": 0, "skip": 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


# --------------------------------------------------------------------------
# 결과 엑셀에서 문의할 주문 고르기
# --------------------------------------------------------------------------

def find_latest_result_excel(directory: Path | None = None) -> Path | None:
    """바탕화면에서 가장 최근 '송장조회결과_*.xlsx'. 엑셀이 열어둔 잠금 파일(~$)은 뺀다."""
    directory = directory or RESULT_DIR
    candidates = [p for p in directory.glob(RESULT_GLOB) if not p.name.startswith("~$")]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_targets(excel_path: str | Path) -> list[InquiryTarget]:
    """'주문일지연' 시트에서 '지난 일수'가 2일인 줄만 읽는다.

    칸은 헤더 이름으로 찾는다(result_excel.STALE_HEADERS) - 시트에 칸을 넣고
    빼도 안 깨지게. 시트가 없으면(그날 지연 건이 없었다) 빈 목록이다.
    """
    wb = load_workbook(excel_path, read_only=True, data_only=True)
    try:
        if STALE_SHEET_NAME not in wb.sheetnames:
            return []
        ws = wb[STALE_SHEET_NAME]
        rows = ws.iter_rows(values_only=True)
        header: list[str] | None = None
        for row in rows:
            if row and "마켓 주문번호" in [str(c).strip() for c in row if c is not None]:
                header = [str(c).strip() if c is not None else "" for c in row]
                break
        if header is None:
            raise ValueError(f"'{STALE_SHEET_NAME}' 시트에서 머리글 줄을 찾지 못했습니다: {excel_path}")
        missing = [h for h in STALE_HEADERS if h in ("마켓 주문번호", "수령인", "지난 일수", "상품URL")
                   and h not in header]
        if missing:
            raise ValueError(f"'{STALE_SHEET_NAME}' 시트에 {missing} 칸이 없습니다: {excel_path}")

        def cell(row, name: str) -> str:
            i = header.index(name) if name in header else -1
            if i < 0 or i >= len(row) or row[i] is None:
                return ""
            return str(row[i]).strip()

        targets: list[InquiryTarget] = []
        seen: set[str] = set()
        for row in rows:
            if not row or cell(row, "마켓 주문번호") == "":
                continue
            if cell(row, "지난 일수") != TARGET_DAYS_TEXT:
                continue
            order_id = cell(row, "마켓 주문번호")
            if order_id in seen:
                continue   # 한 주문이 상품별로 여러 줄이어도 문의는 한 번
            seen.add(order_id)
            targets.append(InquiryTarget(
                order_id=order_id,
                recipient_name=cell(row, "수령인"),
                product_url=cell(row, "상품URL"),
                order_date=cell(row, "주문일"),
                delivery_note=cell(row, "출고/도착예정"),
            ))
        return targets
    finally:
        wb.close()


# --------------------------------------------------------------------------
# 남긴 문의 장부
# --------------------------------------------------------------------------

def load_ledger(path: Path = LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def posted_order_ids(ledger: list[dict]) -> set[str]:
    return {str(e.get("order_id", "")) for e in ledger if e.get("order_id")}


def _append_ledger(entry: dict, path: Path = LEDGER_PATH) -> None:
    """한 건 남길 때마다 바로 적는다 - 도중에 멈춰도 남긴 건은 장부에 있어야 한다."""
    ledger = load_ledger(path)
    ledger.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------

def plan(excel_path: str | Path | None = None) -> tuple[Path | None, list[InquiryTarget], dict[str, list[InquiryTarget]], list[InquiryResult]]:
    """무엇을 문의할지 정한다 (실제로 남기기 전에 GUI가 확인창에 보여주는 용도).

    돌려주는 것: (엑셀 경로, 2일 지남 전체, 사이트별 문의할 건, 문의하지 않고
    넘기는 건의 결과) - 넘기는 건은 이미 남긴 주문, 지원하지 않는 사이트,
    수령인/URL이 빈 줄이다.
    """
    path = Path(excel_path) if excel_path else find_latest_result_excel()
    if path is None or not path.exists():
        return None, [], {}, []
    targets = load_targets(path)
    already = posted_order_ids(load_ledger())
    by_site: dict[str, list[InquiryTarget]] = {}
    skipped: list[InquiryResult] = []
    for t in targets:
        site = t.site_key
        if t.order_id in already:
            skipped.append(InquiryResult(t.order_id, t.recipient_name, site or "", "skip",
                                         "이미 문의를 남긴 주문", t.product_url))
        elif not t.product_url or site is None:
            skipped.append(InquiryResult(t.order_id, t.recipient_name, site or "", "skip",
                                         "상품URL이 없거나 모르는 사이트", t.product_url))
        elif getattr(get_adapter(t.product_url), "post_inquiry", None) is None:
            skipped.append(InquiryResult(t.order_id, t.recipient_name, site, "skip",
                                         f"{site}는 아직 문의 자동화를 지원하지 않음 - 직접 남겨주세요",
                                         t.product_url))
        elif not t.recipient_name:
            skipped.append(InquiryResult(t.order_id, t.recipient_name, site, "skip",
                                         "수령인 이름이 비어 있어 문의 문구를 만들 수 없음", t.product_url))
        else:
            by_site.setdefault(site, []).append(t)
    return path, targets, by_site, skipped


def run(excel_path: str | Path | None = None, *, limit: int | None = None,
        headless: bool = False, dry_run: bool = False, log: LogFn = print) -> InquiryRun:
    """결과 엑셀의 '2일 지남' 건에 문의를 남긴다.

    사이트별로 브라우저 하나를 열어 한 건씩 순서대로 남긴다(같은 사이트에
    몰아치지 않도록 송장조회와 같은 요청 간격을 지킨다). 한 사이트가 로그인
    등으로 막히면 그 사이트의 나머지는 바로 넘기고 다른 사이트는 계속한다.
    dry_run이면 무엇을 남길지만 보여주고 아무것도 남기지 않는다.
    """
    result = InquiryRun()
    path, targets, by_site, skipped = plan(excel_path)
    result.excel_path = path
    result.targets = targets
    result.results.extend(skipped)
    if path is None:
        result.stopped_reason = f"바탕화면에 '{RESULT_GLOB}' 파일이 없습니다. 먼저 송장 조회를 돌려주세요."
        log(result.stopped_reason)
        return result

    log(f"결과 엑셀: {path.name}")
    log(f"'{STALE_SHEET_NAME}' 시트의 {TARGET_DAYS_TEXT} 지남 {len(targets)}건 중 "
        f"문의할 건 {sum(len(v) for v in by_site.values())}건, 넘기는 건 {len(skipped)}건")
    for r in skipped:
        log(f"  - 넘김 {r.order_id} ({r.recipient_name or '이름 없음'}): {r.reason}")

    if limit is not None:
        remaining = limit
        for site in list(by_site):
            by_site[site] = by_site[site][:max(0, remaining)]
            remaining -= len(by_site[site])
            if not by_site[site]:
                del by_site[site]

    if dry_run:
        for site, items in by_site.items():
            for t in items:
                log(f"  [{site}] {t.order_id} {t.recipient_name}: "
                    f"'{_message_for(site, t)}' (미리보기 - 남기지 않음)")
                result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "skip",
                                                    "미리보기 (dry-run)", t.product_url))
        return result

    settings = load_settings()
    for site, items in by_site.items():
        _post_site(site, items, settings=settings, headless=headless, result=result, log=log)
    counts = result.counts()
    log(f"문의 남기기 끝: 성공 {counts['success']} / 실패 {counts['fail']} / 넘김 {counts['skip']}")
    return result


def _message_for(site: str, target: InquiryTarget) -> str:
    adapter = get_adapter(target.product_url)
    make = getattr(adapter, "inquiry_message", None)
    return make(target.recipient_name) if make else f"{target.recipient_name} 배송 언제 시작하나요?"


def _post_site(site: str, items: list[InquiryTarget], *, settings, headless: bool,
               result: InquiryRun, log: LogFn) -> None:
    adapter = get_adapter(items[0].product_url)
    started = time.monotonic()
    with sync_playwright() as p, contextlib.ExitStack() as stack:
        browser, context = browser_mod.get_context(
            p, site, headless=headless,
            context_kwargs=getattr(adapter, "CONTEXT_KWARGS", None))
        stack.callback(browser.close)
        blocked_reason: str | None = None
        next_allowed = 0.0
        try:
            for i, t in enumerate(items, start=1):
                if blocked_reason is not None:
                    result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "skip",
                                                        f"앞 주문에서 막혀 건너뜀: {blocked_reason}",
                                                        t.product_url))
                    continue
                wait = next_allowed - time.monotonic()
                if wait > 0:
                    common.sleep(wait)
                next_allowed = time.monotonic() + rate_limit.request_gap(
                    settings.delay_min, settings.delay_max)
                message = _message_for(site, t)
                try:
                    done = adapter.post_inquiry(context, t.product_url, t.recipient_name,
                                                headless=headless)
                except BlockedError as e:
                    blocked_reason = str(e)
                    result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "fail",
                                                        blocked_reason, t.product_url))
                    log(f"[{site}] {i}/{len(items)} {t.order_id} {t.recipient_name}: 실패 - {e}")
                    continue
                except AdapterError as e:
                    result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "fail",
                                                        str(e), t.product_url))
                    log(f"[{site}] {i}/{len(items)} {t.order_id} {t.recipient_name}: 실패 - {e}")
                    continue
                except Exception as e:  # noqa: BLE001 - 한 건의 예상 못 한 오류가 나머지를 막으면 안 된다
                    result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "fail",
                                                        f"{type(e).__name__}: {e}", t.product_url))
                    log(f"[{site}] {i}/{len(items)} {t.order_id} {t.recipient_name}: 실패 - {e}")
                    continue
                _append_ledger({
                    "order_id": t.order_id,
                    "site": site,
                    "recipient_name": t.recipient_name,
                    "product_url": t.product_url,
                    "order_date": t.order_date,
                    "message": message,
                    "posted_at": datetime.now().isoformat(timespec="seconds"),
                })
                result.results.append(InquiryResult(t.order_id, t.recipient_name, site, "success",
                                                    done, t.product_url))
                log(f"[{site}] {i}/{len(items)} {t.order_id} {t.recipient_name}: 남김 - '{message}'")
        finally:
            with contextlib.suppress(Exception):
                browser_mod.save_state(context, site)
            log(f"[{site}] {len(items)}건에 {time.monotonic() - started:.1f}초 걸렸습니다.")


def summarize(run_result: InquiryRun) -> str:
    counts = run_result.counts()
    lines = [f"문의 남기기 결과: 남김 {counts['success']}건 / 실패 {counts['fail']}건 / 넘김 {counts['skip']}건"]
    fails = [r for r in run_result.results if r.status == "fail"]
    if fails:
        lines.append("")
        lines.append("남기지 못한 주문 (직접 남겨주세요):")
        lines.extend(f"  {r.order_id} ({r.recipient_name}) - {r.reason}" for r in fails)
    unsupported = [r for r in run_result.results
                   if r.status == "skip" and "지원하지 않음" in r.reason]
    if unsupported:
        lines.append("")
        lines.append("아직 자동화하지 않은 사이트 (직접 남겨주세요):")
        lines.extend(f"  {r.order_id} ({r.recipient_name}) - {r.site_key}: {r.product_url}"
                     for r in unsupported)
    if run_result.stopped_reason:
        lines.append("")
        lines.append(run_result.stopped_reason)
    return "\n".join(lines)


def save_run_log(run_result: InquiryRun) -> Path | None:
    """이번 실행의 건별 결과를 logs/inquiry_*.json 으로 남긴다 (장부와는 별개)."""
    if not run_result.results:
        return None
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"inquiry_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "excel": str(run_result.excel_path) if run_result.excel_path else None,
        "results": [asdict(r) for r in run_result.results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

