"""남긴 1:1 문의에 공급사 답변이 달렸는지 확인해 장부에 적고, 결과 엑셀의 '사유' 칸에 싣는다.

왜 필요한가: [문의] 버튼으로 "○○○ 배송 언제 시작하나요?"를 남기면 공급사가
하루 안팎에 "9/15까지 재고 확보 후 발송" 같은 답을 단다. 그 답을 보려면 사람이
사이트마다 문의내역을 열어야 했다. 다음 송장조회 때 그 주문이 '주문일지연'에
그대로 남아 있으면(3일 지남, 4일 지남...) 답변을 같이 보여줘야 '기다리면 되는지'를
한눈에 판단할 수 있다.

무엇을 하나:
  - 장부(logs/inquiries.json)의 문의 중 아직 답변을 못 받은 것(최근
    ANSWER_LOOKBACK_DAYS일 안)을 사이트별로 묶어, 어댑터의 fetch_inquiry_answer로
    문의내역을 읽는다. 어댑터는 장부의 문의번호(확인 문구 안의 '문의번호 N')로 바로
    상세를 열고, 없으면 등록 때와 같은 방법으로 문의내역을 뒤진다.
  - 결과는 장부 항목의 answer_check에 적는다 {checked_at, state, inquiry_id, answer,
    answered_on, problem}. 답변이 온 문의는 다시 묻지 않는다(답변이 바뀌는 일은 없다).
  - 송장조회 파이프라인은 조회가 끝난 뒤 attach()로 장부에 있는 주문(송장을 받은
    성공 건 제외 - 대부분 '주문일지연' 건)만 확인해 ReportEntry.inquiry_note에 싣는다 -
    결과 엑셀 두 시트의 '사유' 칸에 원래 사유 아래 줄로 "[문의 답변 2026.09.11] ..." 또는
    "[문의 09-10 남김 - 답변대기]"가 붙는다(result_excel.compose_reason). 답변이 온 칸은
    진한 녹색 글자.
  - scripts/check_answers.py는 이것을 따로 돌려 최신 결과 엑셀의 '사유' 칸을 제자리에서
    고친다(update_excel) - 파이프라인을 다시 돌리지 않고도 오늘 온 답변을 볼 수 있다.

읽기만 한다 - 문의내역 목록·상세는 GET(롯데아이몰 상세 레이어·NS홈쇼핑 상세는 화면이
쓰는 조회용 POST)이고 등록 주소는 건드리지 않는다. 롯데온은 화면에서 항목을 펼치면
'확인함' 갱신이 나가지만 상세 API는 그런 부작용이 없다.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from .inquiry import LEDGER_PATH, load_ledger, posted_order_ids
from .models import ReportEntry
from .report import is_stale_entry
from .result_excel import SHEET_NAME, STALE_SHEET_NAME, compose_reason, strip_note
from .suppliers import common
from .suppliers.base import AdapterError, BlockedError
from .suppliers.registry import get_adapter

# 이보다 오래전에 남긴 문의는 더 묻지 않는다 - 그때쯤이면 주문이 나갔거나 취소됐다.
ANSWER_LOOKBACK_DAYS = 14
# 사유 칸에 싣는 답변 글자 수 상한 - 사이트 안내문이 길어도 핵심은 앞쪽에 있다.
ANSWER_MAX_CHARS = 700
# 어댑터 확인 문구에서 문의번호를 꺼내는 표식 ("... (문의번호 20538665, 배송/배송일정)").
INQUIRY_ID_PATTERN = re.compile(r"문의번호\s*(\d+)")
NOTE_PREFIX = "[문의"

LogFn = Callable[[str], None]


# --------------------------------------------------------------------------
# 장부
# --------------------------------------------------------------------------

def save_ledger(ledger: list[dict], path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def inquiry_id_of(entry: dict) -> str | None:
    """장부 항목의 확인 문구에서 사이트 문의번호. 없으면(초기 항목) None - 어댑터가 목록을 뒤진다."""
    m = INQUIRY_ID_PATTERN.search(str(entry.get("confirmation") or ""))
    return m.group(1) if m else None


def _posted_on(entry: dict) -> date | None:
    for key in ("posted_at", "order_date"):
        with contextlib.suppress(ValueError, TypeError):
            return datetime.strptime(str(entry.get(key) or "")[:10], "%Y-%m-%d").date()
    return None


def since_of(entry: dict) -> date:
    """문의내역을 어디까지 거슬러 볼지 - 주문일부터 (사람이 먼저 남긴 것도 주문일 이후다)."""
    with contextlib.suppress(ValueError, TypeError):
        return datetime.strptime(str(entry.get("order_date") or "")[:10], "%Y-%m-%d").date()
    return (_posted_on(entry) or date.today()) - timedelta(days=ANSWER_LOOKBACK_DAYS)


def needs_check(entry: dict, today: date | None = None, *, force: bool = False) -> bool:
    """아직 답변을 못 받았고 최근 문의면 사이트에 물어본다."""
    check = entry.get("answer_check") or {}
    if check.get("answer") and not force:
        return False
    posted = _posted_on(entry)
    return posted is not None and (today or date.today()) - posted <= timedelta(days=ANSWER_LOOKBACK_DAYS)


def note_for(entry: dict) -> str:
    """사유 칸에 붙일 메모(답변은 여러 줄) - result_excel.compose_reason이 원래 사유 아래에 붙인다."""
    check = entry.get("answer_check") or {}
    posted = str(entry.get("posted_at") or "")[:10]
    posted_short = posted[5:] if len(posted) == 10 else posted
    if check.get("answer"):
        when = f" {check['answered_on'].replace('-', '.')}" if check.get("answered_on") else ""
        return f"{NOTE_PREFIX} 답변{when}] {_shorten(check['answer'])}"
    if check.get("problem"):
        return f"{NOTE_PREFIX} {posted_short} 남김 - 답변 확인 못 함: {check['problem']}]"
    if check.get("state"):
        return f"{NOTE_PREFIX} {posted_short} 남김 - {check['state']}]"
    return f"{NOTE_PREFIX} {posted_short} 남김 - 답변 아직 확인 안 함]"


def _shorten(text: str) -> str:
    text = text.strip()
    if len(text) <= ANSWER_MAX_CHARS:
        return text
    return text[:ANSWER_MAX_CHARS].rstrip() + " …(이하 생략)"


# --------------------------------------------------------------------------
# 사이트에 물어보기
# --------------------------------------------------------------------------

def refresh(order_ids: set[str] | None = None, *, headless: bool = True, force: bool = False,
            sites: set[str] | None = None, log: LogFn = print) -> dict[str, dict]:
    """장부의 문의에 답변이 달렸는지 사이트에 물어 장부를 갱신하고, 주문번호 -> 장부 항목을 돌려준다.

    order_ids를 주면 그 주문만(파이프라인은 이번 조회에 나온 주문만 준다), 없으면 최근
    문의 전부. 사이트마다 브라우저 하나를 열어 한 건씩 묻고, 사이트 하나가 끝날 때마다
    장부를 저장한다 - 도중에 멈춰도 확인한 것은 남는다. 한 사이트의 실패는 그
    사이트 항목에만 적히고 다른 사이트는 계속한다.
    """
    ledger = load_ledger()
    today = date.today()
    by_site: dict[str, list[dict]] = {}
    for entry in ledger:
        if order_ids is not None and entry.get("order_id") not in order_ids:
            continue
        if sites is not None and entry.get("site") not in sites:
            continue
        if not needs_check(entry, today, force=force):
            continue
        by_site.setdefault(str(entry.get("site") or ""), []).append(entry)

    for site, items in by_site.items():
        adapter = get_adapter(items[0].get("product_url") or "")
        fetch = getattr(adapter, "fetch_inquiry_answer", None)
        if adapter is None or fetch is None:
            log(f"  [{site}] 답변 확인을 지원하지 않는 사이트 - {len(items)}건 건너뜀")
            continue
        try:
            _check_site(site, adapter, items, headless=headless, log=log)
        except Exception as e:  # noqa: BLE001 - 브라우저를 못 여는 등 사이트 단위 실패
            problem = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
            log(f"  [{site}] 답변 확인 실패 - {problem}")
            for entry in items:
                _mark(entry, problem=problem)
        save_ledger(ledger)
    return {str(e.get("order_id")): e for e in ledger if e.get("order_id")}


def _check_site(site: str, adapter, items: list[dict], *, headless: bool, log: LogFn) -> None:
    started = time.monotonic()
    answered = 0
    with sync_playwright() as p, contextlib.ExitStack() as stack:
        if getattr(adapter, "WANTS_CDP_CHROME", False):
            # 지마켓은 번들 크로미엄이 봇 확인에 걸려 문의를 남길 때처럼 진짜 크롬(CDP)으로 읽는다.
            browser_mod.remember_playwright(p)
            context = stack.enter_context(browser_mod.real_chrome_cdp_context(site, p))
        else:
            browser, context = browser_mod.get_context(
                p, site, headless=headless,
                context_kwargs=getattr(adapter, "CONTEXT_KWARGS", None))
            stack.callback(browser.close)

        def _save_state() -> None:
            with contextlib.suppress(Exception):
                browser_mod.save_state(context, site)

        stack.callback(_save_state)
        blocked: str | None = None
        for entry in items:
            label = f"{entry.get('order_id')} {entry.get('recipient_name') or ''}".strip()
            if blocked is not None:
                _mark(entry, problem=f"앞 문의에서 막혀 건너뜀: {blocked}")
                continue
            try:
                found = fetch_one(adapter, context, entry, headless=headless)
            except BlockedError as e:
                blocked = str(e)
                _mark(entry, problem=blocked)
                log(f"  [{site}] {label}: 확인 실패 - {blocked}")
                continue
            except Exception as e:  # noqa: BLE001 - 한 건의 오류가 나머지를 막으면 안 된다
                problem = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
                _mark(entry, problem=problem)
                log(f"  [{site}] {label}: 확인 실패 - {problem}")
                continue
            if found is None:
                _mark(entry, state="문의내역에 없음")
                log(f"  [{site}] {label}: 문의내역에서 찾지 못함")
                continue
            _mark(entry, state=found.get("state") or "", inquiry_id=found.get("inquiry_id"),
                  answer=found.get("answer"), answered_on=found.get("answered_on"))
            if found.get("answer"):
                answered += 1
                log(f"  [{site}] {label}: 답변 {found.get('answered_on') or ''} - "
                    f"{_first_line(found['answer'])}")
            else:
                log(f"  [{site}] {label}: {found.get('state') or '답변 없음'}")
    log(f"  [{site}] {len(items)}건 확인, 답변 {answered}건, {time.monotonic() - started:.1f}초")


def fetch_one(adapter, context, entry: dict, *, headless: bool) -> dict | None:
    """어댑터에 한 건 묻는다 - {inquiry_id, state, written_on, answer, answered_on} 또는 None(문의내역에 없음)."""
    return adapter.fetch_inquiry_answer(
        context, entry.get("product_url") or "", entry.get("recipient_name") or "",
        since=since_of(entry), inquiry_id=inquiry_id_of(entry), headless=headless)


def _mark(entry: dict, *, state: str = "", inquiry_id: str | None = None, answer: str | None = None,
          answered_on: str | None = None, problem: str | None = None) -> None:
    check = {"checked_at": datetime.now().isoformat(timespec="seconds")}
    if problem:
        check["problem"] = problem
    else:
        check["state"] = state
        if inquiry_id:
            check["inquiry_id"] = str(inquiry_id)
        if answer:
            check["answer"] = answer.strip()
            check["answered_on"] = answered_on or ""
    entry["answer_check"] = check


def _first_line(text: str, limit: int = 80) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "").strip()
    return line if len(line) <= limit else line[:limit] + "…"


# --------------------------------------------------------------------------
# 결과에 싣기
# --------------------------------------------------------------------------

def attach(entries: list[ReportEntry], *, headless: bool = True, log: LogFn = print) -> int:
    """문의를 남긴 주문 중 아직 송장을 못 받은 건의 답변을 확인해 inquiry_note에 싣는다. 실은 건수를 돌려준다.

    송장조회 파이프라인이 조회 직후에 부른다. 대상은 '주문일지연' 건이 대부분이지만
    실패·취소/품절로 분류된 건도 장부에 있으면 같이 본다 - 품절 답변("취소만 가능")이
    거기서 나온다. 성공(송장 받음)은 답이 더 필요 없어 뺀다. 장부에 없는 주문(문의를 안
    남겼거나 아직 2일이 안 된 것)은 사이트에 묻지 않으므로 대개 몇 초면 끝난다.
    """
    posted = posted_order_ids(load_ledger())
    targets = [e for e in entries if e.status != "success" and e.order_id in posted]
    wanted = {e.order_id for e in targets}
    if not wanted:
        return 0
    stale_count = sum(1 for e in targets if is_stale_entry(e))
    log(f"  문의를 남긴 주문 {len(wanted)}건(주문일지연 {stale_count}건)의 답변을 확인합니다.")
    by_id = refresh(wanted, headless=headless, log=log)
    count = 0
    for e in targets:
        ledger_entry = by_id.get(e.order_id)
        if ledger_entry is None:
            continue
        e.inquiry_note = note_for(ledger_entry)
        count += 1
    return count


def update_excel(path: str | Path, by_id: dict[str, dict]) -> int:
    """이미 저장된 송장조회 결과 엑셀의 '사유' 칸에 문의 메모를 제자리에서 붙인다 (고친 칸 수).

    두 시트('송장조회결과'·'주문일지연') 모두, '마켓 주문번호'가 장부에 있는 줄만.
    전에 붙인 메모("[문의 ...]" 첫 줄)는 떼고 새로 붙여 여러 번 돌려도 한 줄만 남는다.
    """
    path = Path(path)
    wb = load_workbook(path)
    changed = 0
    try:
        for sheet_name in (SHEET_NAME, STALE_SHEET_NAME):
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            header_row, col_id, col_reason = _find_columns(ws)
            if header_row is None:
                continue
            for row in ws.iter_rows(min_row=header_row + 1):
                order_id = str(row[col_id].value or "").strip()
                entry = by_id.get(order_id)
                if entry is None:
                    continue
                cell = row[col_reason]
                new_value = compose_reason(note_for(entry), strip_note(str(cell.value or "")))
                if cell.value != new_value:
                    cell.value = new_value
                    changed += 1
        if changed:
            wb.save(path)
    finally:
        wb.close()
    return changed


def _find_columns(ws) -> tuple[int | None, int, int]:
    for row in ws.iter_rows(min_row=1, max_row=5):
        values = [str(c.value).strip() if c.value is not None else "" for c in row]
        if "마켓 주문번호" in values and "사유" in values:
            return row[0].row, values.index("마켓 주문번호"), values.index("사유")
    return None, -1, -1
