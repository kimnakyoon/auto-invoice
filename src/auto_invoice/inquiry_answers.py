"""남긴 1:1 문의에 공급사 답변이 달렸는지 확인해 장부에 적고, 송장조회 결과 엑셀의 '사유' 칸에 싣는다.

왜 필요한가: [문의] 버튼으로 "○○○ 배송 언제 시작하나요?"를 남기면 공급사가
하루 안팎에 "9/15까지 재고 확보 후 발송" 같은 답을 단다. 그 답을 보려면 사람이
사이트마다 문의내역을 열어야 했다. 다음 날 [문의]를 누를 때 그 답을 같이 가져와
결과 엑셀에 붙여두면 '주문일지연'에 남은 건을 기다리면 되는지 한눈에 판단할 수 있다.

언제 확인하나 - [문의] 버튼(inquiry.run)을 눌렀을 때다 (사용자 요청 2026-09-11 - 그 전엔
송장조회가 조회 직후에 했다). 문의를 남기는 김에 전에 남긴 문의의 답을 읽는다:
  - 사이트별로 문의를 다 남기고 브라우저를 닫기 전에 check_site_with_context가 그
    사이트의 답 없는 문의를 같은 로그인 세션으로 읽는다 - 브라우저를 다시 열지 않는다.
  - 이번에 남길 문의가 없는 사이트는 refresh가 사이트마다 스레드 하나로 브라우저를
    열어 병렬로 읽는다(2026-09-11 실측 장부 71건 4.3초).
  - 오늘 남긴 문의는 묻지 않는다 - 답이 있을 수 없다(2일 지남은 그날 남기는 건이고,
    다음 날부터 답을 본다는 사용자 기준).
  - 남길 문의가 하나도 없어도 [문의]를 누르면 답변 확인만 한다.

무엇을 하나:
  - 장부(logs/inquiries.json)의 문의 중 아직 답변을 못 받은 것(최근
    ANSWER_LOOKBACK_DAYS일 안)을 사이트별로 묶어, 어댑터의 fetch_inquiry_answer로
    문의내역을 읽는다. 어댑터는 장부의 문의번호(확인 문구 안의 '문의번호 N')로 바로
    상세를 열고, 없으면 등록 때와 같은 방법으로 문의내역을 뒤진다.
  - 결과는 장부 항목의 answer_check에 적는다 {checked_at, state, inquiry_id, answer,
    answered_on, problem}. 답변이 온 문의는 다시 묻지 않는다(답변이 바뀌는 일은 없다).
  - update_excel이 [문의]가 읽은 송장조회 결과 엑셀(바탕화면 최신)의 두 시트 '사유' 칸에
    원래 사유 아래 줄로 "[문의 답변 2026.09.11] ..."(진한 녹색) 또는
    "[문의 09-10 남김 - 답변대기]"를 제자리에서 붙인다(result_excel.compose_reason). 전에
    붙인 메모는 떼고 다시 붙여 여러 번 돌려도 한 줄만 남는다.
  - scripts/check_answers.py는 같은 확인을 [문의] 없이 따로 돌린다.

읽기만 한다 - 문의내역 목록·상세는 GET(롯데아이몰 상세 레이어·NS홈쇼핑 상세는 화면이
쓰는 조회용 POST)이고 등록 주소는 건드리지 않는다. 롯데온은 화면에서 항목을 펼치면
'확인함' 갱신이 나가지만 상세 API는 그런 부작용이 없다.
"""

from __future__ import annotations

import contextlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

from . import browser as browser_mod
from .inquiry import LEDGER_LOCK, LEDGER_PATH, load_ledger, posted_order_ids, save_ledger
from .result_excel import SHEET_NAME, STALE_SHEET_NAME, compose_reason, strip_note, style_reason_cell
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
# 장부는 여러 스레드(사이트마다 하나)가 동시에 고치므로 읽기-고치기-쓰기를 잠금 안에서 한다.


def _ledger_by_id() -> dict[str, dict]:
    return {str(e.get("order_id")): e for e in load_ledger() if e.get("order_id")}


def _record_checks(checks: dict[str, dict]) -> dict[str, dict]:
    """주문번호별 확인 결과를 장부에 적어 저장하고, 주문번호 -> 장부 항목을 돌려준다."""
    with LEDGER_LOCK:
        ledger = load_ledger()
        for entry in ledger:
            check = checks.get(str(entry.get("order_id")))
            if check is not None:
                entry["answer_check"] = check
        if checks:
            save_ledger(ledger)
    return {str(e.get("order_id")): e for e in ledger if e.get("order_id")}


def pending_by_site(*, order_ids: set[str] | None = None, sites: set[str] | None = None,
                    force: bool = False, skip_today: bool = False) -> dict[str, list[dict]]:
    """사이트에 물어볼 장부 항목을 사이트별로 묶는다 (답 없는 최근 문의).

    skip_today면 오늘 남긴 문의는 뺀다 - [문의] 실행이 방금 남긴 것에는 답이 있을 수
    없다(다음 날부터 본다는 사용자 기준 2026-09-11).
    """
    today = date.today()
    by_site: dict[str, list[dict]] = {}
    for entry in load_ledger():
        if order_ids is not None and entry.get("order_id") not in order_ids:
            continue
        if sites is not None and entry.get("site") not in sites:
            continue
        if not needs_check(entry, today, force=force):
            continue
        if skip_today and _posted_on(entry) == today:
            continue
        by_site.setdefault(str(entry.get("site") or ""), []).append(entry)
    return by_site


def refresh(order_ids: set[str] | None = None, *, headless: bool = True, force: bool = False,
            sites: set[str] | None = None, skip_today: bool = False,
            log: LogFn = print) -> dict[str, dict]:
    """장부의 문의에 답변이 달렸는지 사이트에 물어 장부를 갱신하고, 주문번호 -> 장부 항목을 돌려준다.

    order_ids를 주면 그 주문만, 없으면 최근 문의 전부. 사이트마다 스레드 하나가 자기
    브라우저를 열어 한 건씩 묻는다 - 송장조회(orchestrator)와 같은 병렬 구조라 전체
    시간은 가장 느린 사이트 하나만큼이다(2026-09-11 실측: 7개 사이트 1건씩 순차 9.1초 →
    병렬 3초 안팎). 사이트 하나가 끝날 때마다 장부를 저장해 도중에 멈춰도 확인한 것은
    남고, 한 사이트의 실패는 그 사이트 항목에만 적힌다.
    """
    by_site = pending_by_site(order_ids=order_ids, sites=sites, force=force, skip_today=skip_today)

    def _one_site(site: str, items: list[dict]) -> None:
        adapter = get_adapter(items[0].get("product_url") or "")
        if adapter is None or getattr(adapter, "fetch_inquiry_answer", None) is None:
            log(f"  [{site}] 답변 확인을 지원하지 않는 사이트 - {len(items)}건 건너뜀")
            return
        try:
            checks = _check_site(site, adapter, items, headless=headless, log=log)
        except Exception as e:  # noqa: BLE001 - 브라우저를 못 여는 등 사이트 단위 실패
            problem = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
            log(f"  [{site}] 답변 확인 실패 - {problem}")
            checks = {str(entry.get("order_id")): _check(problem=problem) for entry in items}
        _record_checks(checks)

    if by_site:
        with ThreadPoolExecutor(max_workers=len(by_site)) as pool:
            futures = [pool.submit(_one_site, site, items) for site, items in by_site.items()]
            for future in futures:
                future.result()
    return _ledger_by_id()


def _check_site(site: str, adapter, items: list[dict], *, headless: bool, log: LogFn) -> dict[str, dict]:
    """브라우저를 열어 한 사이트의 문의들을 묻는다 (주문번호 -> 확인 결과). 스레드마다 자기 Playwright."""
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
        return check_with_context(site, adapter, context, items, headless=headless, log=log)


def check_with_context(site: str, adapter, context, items: list[dict], *, headless: bool,
                       log: LogFn) -> dict[str, dict]:
    """열려 있는 컨텍스트로 장부 항목들을 한 건씩 묻는다 (주문번호 -> 확인 결과).

    송장조회가 그 사이트 브라우저를 아직 열어둔 채로 부르면(orchestrator._lookup_site)
    브라우저를 다시 열지도, 로그인을 다시 하지도 않는다. 로그인이 막히면(BlockedError)
    남은 건은 바로 넘긴다.
    """
    started = time.monotonic()
    answered = 0
    checks: dict[str, dict] = {}
    blocked: str | None = None
    for entry in items:
        order_id = str(entry.get("order_id"))
        label = f"{order_id} {entry.get('recipient_name') or ''}".strip()
        if blocked is not None:
            checks[order_id] = _check(problem=f"앞 문의에서 막혀 건너뜀: {blocked}")
            continue
        try:
            found = fetch_one(adapter, context, entry, headless=headless)
        except BlockedError as e:
            blocked = str(e)
            checks[order_id] = _check(problem=blocked)
            log(f"  [{site}] {label}: 답변 확인 실패 - {blocked}")
            continue
        except Exception as e:  # noqa: BLE001 - 한 건의 오류가 나머지를 막으면 안 된다
            problem = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
            checks[order_id] = _check(problem=problem)
            log(f"  [{site}] {label}: 답변 확인 실패 - {problem}")
            continue
        if found is None:
            checks[order_id] = _check(state="문의내역에 없음")
            log(f"  [{site}] {label}: 문의내역에서 찾지 못함")
            continue
        checks[order_id] = _check(state=found.get("state") or "", inquiry_id=found.get("inquiry_id"),
                                  answer=found.get("answer"), answered_on=found.get("answered_on"))
        if found.get("answer"):
            answered += 1
            log(f"  [{site}] {label}: 답변 {found.get('answered_on') or ''} - "
                f"{_first_line(condense_answer(found['answer'], str(entry.get('message') or '')))}")
        else:
            log(f"  [{site}] {label}: {found.get('state') or '답변 없음'}")
    log(f"  [{site}] 문의 {len(items)}건 확인, 답변 {answered}건, {time.monotonic() - started:.1f}초")
    return checks


def fetch_one(adapter, context, entry: dict, *, headless: bool) -> dict | None:
    """어댑터에 한 건 묻는다 - {inquiry_id, state, written_on, answer, answered_on} 또는 None(문의내역에 없음)."""
    return adapter.fetch_inquiry_answer(
        context, entry.get("product_url") or "", entry.get("recipient_name") or "",
        since=since_of(entry), inquiry_id=inquiry_id_of(entry), headless=headless)


def _check(*, state: str = "", inquiry_id: str | None = None, answer: str | None = None,
           answered_on: str | None = None, problem: str | None = None) -> dict:
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
    return check


def _first_line(text: str, limit: int = 80) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "").strip()
    return line if len(line) <= limit else line[:limit] + "…"


# --------------------------------------------------------------------------
# 결과에 싣기
# --------------------------------------------------------------------------

def check_site_with_context(site: str, adapter, context, *, headless: bool,
                            log: LogFn = print) -> dict[str, dict]:
    """[문의]가 한 사이트의 문의를 다 남기고 브라우저를 닫기 전에 부른다 - 그 사이트의 답 없는 문의를 읽어 장부에 적는다.

    브라우저·로그인 세션을 그대로 쓰니 사이트마다 브라우저를 다시 여는 refresh보다
    싸다. 오늘 남긴 문의는 뺀다. 주문번호 -> 확인 결과를 돌려주고, 여기서 무엇이
    잘못돼도 문의 남기기 결과는 그대로다(문제는 장부 항목의 problem에 적힌다).
    """
    if getattr(adapter, "fetch_inquiry_answer", None) is None:
        return {}
    items = pending_by_site(sites={site}, skip_today=True).get(site) or []
    if not items:
        return {}
    try:
        checks = check_with_context(site, adapter, context, items, headless=headless, log=log)
    except Exception as e:  # noqa: BLE001
        problem = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
        log(f"  [{site}] 답변 확인 실패 - {problem}")
        checks = {str(entry.get("order_id")): _check(problem=problem) for entry in items}
    _record_checks(checks)
    return checks


def answered_ids(by_id: dict[str, dict]) -> set[str]:
    """답변을 받은 문의의 주문번호들 (실행 전후를 견줘 '새로 온 답변'을 고르는 데 쓴다)."""
    return {oid for oid, e in by_id.items() if (e.get("answer_check") or {}).get("answer")}


def ledger_by_id() -> dict[str, dict]:
    return _ledger_by_id()


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
                note = note_for(entry)
                new_value = compose_reason(note, strip_note(str(cell.value or "")))
                if cell.value != new_value:
                    cell.value = new_value
                    style_reason_cell(cell, note)
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
