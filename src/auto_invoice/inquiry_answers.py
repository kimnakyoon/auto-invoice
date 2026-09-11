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
from . import order_date as order_date_mod
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
        return f"{NOTE_PREFIX} 답변{when}] {_shorten(condense_answer(check['answer'], str(entry.get('message') or '')))}"
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
# 답변에서 인사말·상투구를 걷어내고 핵심 문장만 남기기 (사용자 요청 2026-09-11)
# --------------------------------------------------------------------------
# 장부에는 답변 원문을 그대로 두고 사유 칸에 실을 때만 줄인다 - 규칙을 고쳐도 다시
# 읽어올 필요가 없다. 규칙은 2026-09-11까지 받은 답변 51건(7개 사이트)으로 맞췄다:
#   - 줄을 문장으로 나눈다. 사이트가 문장 중간에서 줄을 끊어 보내기도 해서(GS샵
#     "먼저 문의하신 내용에 대해 바로 확인해" / "드리지 못해 죄송합니다.") 마침표·
#     물음표·'다/요'로 끝나지 않고 글머리표(■ - ·)나 숫자로 끝나지도 않는 줄은
#     다음 줄에 이어 붙인다. 지마켓처럼 한 줄에 여러 문장을 붙여 보내는 것은
#     '~니다' 뒤 공백에서도 나눈다.
#   - 우리가 남긴 문구가 답변 앞에 그대로 인용돼 오는 것(지마켓)은 지운다.
#   - 상투 문장(인사·감사·사과·"노력하겠습니다"·추가 문의 안내·일반 양해·"조금만
#     기다려")은 버리되, **숫자가 든 문장은 남긴다** - "9/11일까지 배송예정이니
#     조금만 기다려 주시기 바랍니다"처럼 날짜·송장번호가 있는 문장이 핵심이다.
#   - 다 걸러져 남는 게 없으면 원문을 그대로 쓴다.
ANSWER_SENTENCE_END = re.compile(r"[.!?…]\)?$|[다요죠]$|\d$")
ANSWER_BULLET_START = re.compile(r"^[■\-·•*\[(]")
ANSWER_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|(?<=니다)[.!?]?\s+(?=\S)")
ANSWER_LEADING_JUNK = re.compile(r"^[\s:;,.)\-]+")
ANSWER_BOILERPLATE = re.compile(
    "|".join([
        r"안녕하세요", r"인사드립니다",
        r"감사합니다", r"감사드립니다", r"감사드리며", r"이용해 주셔서", r"주문해주셔서",
        r"죄송합니다", r"죄송스럽", r"죄송하다는", r"사과드립니다", r"사과의 말씀",
        r"불편을 드려", r"불편 드려", r"기다리시게", r"기다리게 해", r"기다리셨", r"기다려주셔서",
        r"노력하겠습니다", r"최선을 다", r"되겠습니다", r"보답하겠", r"약속드립니다", r"도와드리겠습니다",
        r"행복한", r"편안한 하루", r"편안하고", r"좋은 하루", r"건강하시", r"기원합니다", r"되십시오", r"보내세요",
        r"평온하고", r"가득하길",
        r"추가 문의", r"다른 문의", r"문의 부탁", r"고객센터 ☎", r"유선 안내", r"발신번호", r"연락드릴 수 있는 점",
        r"자세한 배송현황", r"교환/반품이 필요", r"마이롯데'를 통해", r"확인하실 수 있습니다",
        r"양해 부탁", r"양해 바랍", r"참고 부탁", r"사정에 따라", r"달라질 수 있", r"변동될 수 있",
        r"품절될 수 있", r"지연될 수 있", r"발생될 수 있", r"소요되는 점",
        r"조금만 기다려", r"조금만 더 기다려", r"기다려 주시기", r"기다려주세요",
        r"다시 연락드리", r"즉시 고객님께 연락", r"^\[.*\]$",
    ]))
ANSWER_HAS_DIGIT = re.compile(r"\d")
# 전화번호(1899-4500, 1588-2121)는 날짜·송장번호가 아니다 - 숫자 보호에서 뺀다.
ANSWER_PHONE = re.compile(r"\d{2,4}-\d{3,4}(?:-\d{4})?")
# 소속 소개("롯데홈쇼핑 ○○○입니다", "NS홈쇼핑 상담사 ○○○입니다") - '~입니다'로 끝나면서
# 배송 정보 낱말이 하나도 없는 짧은 문장. "금일 출고 예정입니다"는 정보 낱말이 있어 남는다.
ANSWER_INTRO_END = re.compile(r"입니다[.!]?$")
ANSWER_INFO_WORD = re.compile(r"출고|발송|배송|예정|송장|재고|품절|입고|취소|환불|확인|수급|접수|문자|이동|택배|주문")
ANSWER_LEADING_CUSTOMER = re.compile(r"^고객님[,.!]?\s+")


def _is_boilerplate(sentence: str) -> bool:
    if ANSWER_HAS_DIGIT.search(ANSWER_PHONE.sub("", sentence)):
        return False    # 날짜·송장번호가 든 문장은 어떤 상투구가 섞여 있어도 남긴다
    if ANSWER_BOILERPLATE.search(sentence):
        return True
    return bool(ANSWER_INTRO_END.search(sentence)) and not ANSWER_INFO_WORD.search(sentence)


def _answer_sentences(text: str) -> list[str]:
    """줄을 이어 붙여 문장 단위로 나눈다 (헤더 주석의 규칙)."""
    joined: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if (joined and not ANSWER_SENTENCE_END.search(joined[-1])
                and not ANSWER_BULLET_START.match(line) and not ANSWER_BULLET_START.match(joined[-1])):
            joined[-1] = f"{joined[-1]} {line}"
        else:
            joined.append(line)
    sentences: list[str] = []
    for line in joined:
        for part in ANSWER_SENTENCE_SPLIT.split(line):
            part = ANSWER_LEADING_JUNK.sub("", part or "").strip()
            part = ANSWER_LEADING_CUSTOMER.sub("", part)   # "안녕하세요. 고객님" 줄이 다음 줄에 붙은 흔적
            if part:
                sentences.append(part)
    return sentences


def condense_answer(text: str, message: str = "") -> str:
    """답변에서 인사말·상투구를 걷어내고 핵심 문장만 (없으면 원문)."""
    body = text.replace(message, " ") if message else text
    kept = [s for s in _answer_sentences(body) if not _is_boilerplate(s)]
    return "\n".join(kept) if kept else re.sub(r"\n{2,}", "\n", text.strip())


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

    '2일 지남'은 뺀다(사용자 기준 2026-09-11): 2일 지남은 이 조회가 끝난 뒤 [문의]로
    그날 남기는 건이라 확인할 답변이 없다. 3일 지남부터가 '2일이던 날 남긴 문의'의
    답을 볼 차례다.
    """
    posted = posted_order_ids(load_ledger())
    targets = [e for e in entries
               if e.status != "success" and e.order_id in posted and _old_enough_to_answer(e)]
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


# 조회 직후 답변을 볼 최소 '지난 일수' - 2일 지남은 그날 문의를 남기는 건이라 뺀다.
ANSWER_MIN_DAYS = order_date_mod.STALE_DAYS + 1


def _old_enough_to_answer(entry: ReportEntry) -> bool:
    """주문일을 모르면(실패 건 등) 장부에 있다는 것만으로 본다 - 문의를 남긴 건 확실하다."""
    days = order_date_mod.days_since(entry.order_date)
    return days is None or days >= ANSWER_MIN_DAYS


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
