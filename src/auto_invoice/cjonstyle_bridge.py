"""CJ온스타일 주문의 배송 조회를, Claude Code(`claude -p --chrome`)를 통해
사용자의 실제 크롬 브라우저로 수행한다.

CJ온스타일(base.cjonstyle.com)은 로그인 페이지의 Cloudflare Turnstile이
Playwright로 띄운 브라우저를 감지해서 막기 때문에(suppliers/cjonstyle.py의
docstring 참고, 실제 로그인된 크롬 세션의 쿠키를 옮겨와도 마찬가지였다), 이
사이트만은 Playwright가 아니라 claude-in-chrome 확장으로 사용자의 실제 크롬
브라우저를 조작해서 조회해야 한다. 그 조작은 Claude 모델 자신이 도구를
호출해서 수행해야 하므로(브라우저 확장 프로토콜은 Claude Code 세션 안에서만
호출 가능), 이 모듈은 `claude -p --chrome` 하위 프로세스를 실행해서 조회를
위임하고, `--json-schema`로 구조화된 결과를 받는다.

사전 조건: 이 저장소의 .claude/settings.local.json에 아래 권한이 허용되어
있어야 한다 (Claude Code 오토 모드 세션은 재귀적 claude 호출에 권한을 새로
부여하는 것 자체를 거부하므로, 사용자가 오토 모드가 아닌 세션에서 직접
추가해야 한다):
    "Bash(claude -p --chrome *)", "ToolSearch",
    "mcp__claude-in-chrome__tabs_context_mcp",
    "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__tabs_close_mcp",
    "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__computer",
    "mcp__claude-in-chrome__get_page_text", "mcp__claude-in-chrome__find"
권한이 없으면 하위 프로세스가 도구 호출마다 승인을 기다리다 타임아웃난다.

주문 하나 조회에 사람이 크롬으로 직접 확인했을 때와 동일한 결과(주문
20260826017435 -> 송장 316726014614/롯데택배)가 나오는 것을 실제로
확인했다.

속도: 처음엔 주문 여러 건을 프롬프트 하나에 넣어 순차 처리했는데(4건에
140초), claude-in-chrome은 세션마다 독립된 탭 그룹을 쓰는 것을 확인해서
주문 1건당 별도의 `claude -p --chrome` 프로세스를 병렬로 띄우는 방식으로
바꿨다. 실측(같은 4건 기준): 동시 2개 94초, 동시 3개 65초, 동시 4개는
크롬 확장을 서로 잡으려다 충돌해 전부 실패했다. 그래서 동시 3개로 제한하고,
그래도 충돌로 실패하는 건이 있으면 마지막에 순차로 한 번 더 재시도한다.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import order_date as order_date_mod
from .models import PendingOrder

CJONSTYLE_DOMAIN = "cjonstyle.com"

# 주문 1건짜리 하위 프로세스가 대략 30~90초 걸리는 것을 확인했다(크롬 조작 +
# 모델 추론 시간).
_TIMEOUT_SEC = 180
# 동시에 띄울 하위 프로세스 수. 4개를 "동시에" 띄웠을 때는 크롬 확장을 서로
# 잡으려다 충돌해서("Tabs cannot be edited right now", 도메인 권한 없음 등)
# 전부 실패했지만, 아래 _STAGGER_SEC만큼 시작 시점을 어긋나게 하면 탭 그룹
# 생성이 겹치지 않아 안정적으로 동작한다(위 docstring의 실측 참고).
_MAX_WORKERS = 4
# 각 프로세스의 시작 시점을 이만큼씩 늦춰 탭 그룹 생성 충돌을 피한다.
_STAGGER_SEC = 3.0

_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["success", "not_yet", "cancelled", "fail"]},
                    "tracking_no": {"type": "string"},
                    "courier": {"type": "string"},
                    "reason": {"type": "string"},
                    "order_date": {"type": "string"},
                },
                "required": ["order_id", "status"],
            },
        }
    },
    "required": ["results"],
}

_PROMPT_TEMPLATE = """너는 CJ온스타일(base.cjonstyle.com) 주문의 배송 송장번호를 조회하는 작업을 수행한다.
사용자의 실제 Chrome 브라우저(claude-in-chrome 확장, 이미 CJ온스타일에 로그인되어 있음)를 사용해라.
속도가 중요하니 아래 지시를 최대한 그대로 따르고, 불필요하게 추가로 확인하거나 재시도하지 마라.

조회할 주문 (JSON 배열): {orders_json}

먼저 ToolSearch로 "select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__find,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__browser_batch" 로 도구를 한 번에 로드하고, tabs_context_mcp(createIfEmpty:true)로 탭을 준비해라(이미 있는 탭이 있으면 새로 만들지 말고 그 탭을 재사용해라).

각 주문에 대해 순서대로, 매번 browser_batch로 최대한 묶어서 호출해라(왕복 횟수를 줄이는 게 속도에 가장 중요하다):
1. browser_batch 한 번으로 [navigate(product_url), computer(wait 1.5초), get_page_text]를 순서대로 실행한다.
1-1. 그 페이지 텍스트에서 주문일자("주문일"/"주문일자"/"결제일" 옆에 적힌 날짜)를 찾아 order_date에 "YYYY-MM-DD" 형식으로 기록해라. 배송예정일이나 다른 날짜를 넣지 말고, 확실하지 않으면 order_date를 아예 넣지 마라. 이 값은 아래 어떤 status로 끝나든 같이 기록한다.
2. 그 결과에서 URL이 로그인 페이지이거나 "/p/myzone/" 경로를 벗어나 홈으로 리다이렉트됐으면 status="fail", reason="로그인 필요"로 기록하고 다음 주문으로 넘어간다.
3. "배송조회" 버튼이 없고 "상품준비중"/"배송준비중"/"결제완료"/"주문접수" 같은 문구가 있으면 status="not_yet"으로 기록하고 다음 주문으로 넘어간다.
3-1. "배송조회" 버튼이 없고 주문상태에 "취소" 또는 "품절"이 있으면 status="cancelled", reason에 그 상태 문구를 그대로 넣고 다음 주문으로 넘어간다. 주의: 화면 어딘가에 있는 "주문취소" 같은 버튼 이름이 아니라, 이 주문의 상태 표시일 때만 그렇게 판단해라.
4. "배송조회" 버튼이 있으면(여러 개면 order_option과 가장 관련있는 상품 옆의 것을 find로 찾아서) browser_batch 한 번으로 [computer(click), computer(wait 1.5초), get_page_text]를 순서대로 실행한다. 클릭하면 같은 탭에서 deliveryTracking/sheet 페이지로 이동한다.
5. 그 get_page_text 결과에서 "송장번호" 다음 줄의 숫자, "택배업체" 다음 줄의 첫 단어(택배사명)를 읽는다.
6. 택배사명 정규화: "대한통운" 또는 "CJ"가 포함되면 "CJ대한통운", "롯데"가 포함되면 "롯데택배", "DELIBOX"가 포함되면 "딜리박스"로 바꾼다. 매칭 안되면 원문 그대로 쓴다.
7. status="success", tracking_no, courier를 기록한다.

중요: 모든 주문 처리가 끝나면(실패했거나 중간에 문제가 생긴 경우에도 반드시) 이 작업에서 사용한 탭을 tabs_close_mcp로 전부 닫아라. 사용자가 실행 후 크롬 창이 남지 않기를 원한다. 결과 JSON을 출력하기 전에 닫아야 한다.

모든 주문 처리 후, 결과를 JSON으로만 출력해라 (설명 텍스트 없이)."""


@dataclass
class CjonstyleLookupResult:
    order_id: str
    status: str  # "success" | "not_yet" | "cancelled" | "fail"
    tracking_no: str | None = None
    courier: str | None = None
    reason: str | None = None
    # 주문상세에 적혀 있던 주문일 ("2026-08-26"). 못 읽었으면 None.
    order_date: str | None = None


def filter_cjonstyle_orders(orders: list[PendingOrder]) -> list[PendingOrder]:
    return [o for o in orders if CJONSTYLE_DOMAIN in o.product_url]


def _lookup_one(order: PendingOrder, stagger_sec: float = 0.0) -> CjonstyleLookupResult:
    """주문 1건을 claude -p --chrome 하위 프로세스 1개로 조회한다.

    stagger_sec: 시작 전에 대기할 시간. 여러 프로세스가 동시에 크롬 탭 그룹을
    만들려다 충돌하는 것을 피하려고 호출자가 순번에 비례해 넣어준다.
    """
    if stagger_sec:
        time.sleep(stagger_sec)

    orders_payload = [{"order_id": order.order_id, "product_url": order.product_url, "order_option": order.order_option}]
    prompt = _PROMPT_TEMPLATE.format(orders_json=json.dumps(orders_payload, ensure_ascii=False))

    cmd = [
        "claude",
        "-p",
        prompt,
        "--chrome",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(_SCHEMA, ensure_ascii=False),
        "--model",
        "sonnet",
        "--effort",
        "low",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return CjonstyleLookupResult(order_id=order.order_id, status="fail", reason=f"{_TIMEOUT_SEC}초 내에 응답이 없었습니다(시간초과)")

    if proc.returncode != 0:
        return CjonstyleLookupResult(
            order_id=order.order_id,
            status="fail",
            reason=f"claude -p --chrome 실행 실패(종료코드 {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}",
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return CjonstyleLookupResult(order_id=order.order_id, status="fail", reason=f"응답을 해석하지 못했습니다: {proc.stdout[:300]}")

    if envelope.get("is_error"):
        return CjonstyleLookupResult(order_id=order.order_id, status="fail", reason=str(envelope.get("result"))[:300])

    structured = envelope.get("structured_output") or {}
    raw_results = structured.get("results") or []
    if not raw_results:
        return CjonstyleLookupResult(order_id=order.order_id, status="fail", reason="결과가 비어있습니다")

    r = raw_results[0]
    return CjonstyleLookupResult(
        order_id=str(r.get("order_id") or order.order_id),
        status=r.get("status", "fail"),
        tracking_no=r.get("tracking_no"),
        courier=r.get("courier"),
        reason=r.get("reason"),
        order_date=r.get("order_date"),
    )


def lookup_via_chrome(orders: list[PendingOrder], on_progress=None) -> list[CjonstyleLookupResult]:
    """주문마다 별도의 claude -p --chrome 프로세스를 동시에 띄워 조회한다.

    호출자가 이미 CJ온스타일 주문만 걸러서 넘겨야 한다(filter_cjonstyle_orders).
    한 건이 실패해도 나머지 건 처리에는 영향을 주지 않는다. 병렬 실행 중
    크롬 확장 충돌로 실패한 건이 있을 수 있어(위 docstring 참고), 실패한
    건만 마지막에 순차로 한 번 더 시도한다.

    on_progress(끝난건수, 전체건수, 주문번호, 재시도여부): 한 건이 끝날 때마다
    부른다. 병렬로 도니까 주문 순서가 아니라 '끝난 순서'로 세어서 넘긴다.
    마지막 순차 재시도는 재시도여부=True로, 재시도 대상 안에서 다시 센다.
    """
    if not orders:
        return []

    total = len(orders)
    workers = min(total, _MAX_WORKERS)
    # 처음 동시에 뜨는 workers개만 시작 시점을 어긋나게 하면 된다(그 뒤 주문들은
    # 앞 프로세스가 끝난 뒤에 시작하므로 자연히 겹치지 않는다).
    staggers = [(i % workers) * _STAGGER_SEC for i in range(total)]

    done = 0
    lock = threading.Lock()

    def lookup_and_count(order, stagger):
        nonlocal done
        result = _lookup_one(order, stagger)
        if on_progress is not None:
            with lock:
                done += 1
                finished = done
            on_progress(finished, total, order.order_id, False)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lookup_and_count, orders, staggers))

    retry_targets = [i for i, r in enumerate(results) if r.status == "fail"]
    for n, i in enumerate(retry_targets, start=1):
        results[i] = _lookup_one(orders[i])
        if on_progress is not None:
            on_progress(n, len(retry_targets), orders[i].order_id, True)

    return results


def process_orders(report, input_path: str, output_path: str, log=print,
                   on_progress=None) -> int:
    """orchestrator가 스킵한 CJ온스타일 주문을 크롬으로 조회해 report와
    업로드 파일에 반영한다.

    CJ온스타일은 registry.py에 어댑터가 등록되어 있지 않아(Cloudflare가
    Playwright 자동화를 차단 - suppliers/cjonstyle.py 참고) orchestrator가
    전부 "등록된 어댑터 없음"으로 스킵한다. 그 스킵 기록을 지우고 실제
    조회 결과로 다시 채운다.

    반환: 업로드 파일에 새로 추가된 성공 건수.
    """
    from .shopmine import excel_io

    all_orders = excel_io.read_pending_orders(input_path)
    cj_orders = filter_cjonstyle_orders(all_orders)
    if not cj_orders:
        return 0

    log(f"CJ온스타일 {len(cj_orders)}건을 실제 크롬 브라우저로 확인 중입니다 "
        "(시간이 다소 걸릴 수 있습니다)...")

    cj_order_ids = {o.order_id for o in cj_orders}
    recipient_by_id = {o.order_id: o.recipient_name for o in cj_orders}
    report.entries = [
        e for e in report.entries if not (e.order_id in cj_order_ids and e.status == "skip")
    ]

    try:
        results = lookup_via_chrome(cj_orders, on_progress=on_progress)
    except Exception as e:  # noqa: BLE001
        log(f"CJ온스타일 확인 중 오류가 발생해 전부 건너뜁니다: {e}")
        for order in cj_orders:
            report.fail(order.order_id, f"CJ온스타일 크롬 조회 실패: {e}",
                        recipient_name=order.recipient_name)
        return 0

    upload_rows: list[tuple[str, str, str | None]] = []
    for r in results:
        # 크롬으로 읽어온 주문일도 다른 공급사와 똑같이 결과에 싣는다
        # (오래된 주문을 따로 모으는 데 쓴다 - order_date.py 참고).
        ordered_on = order_date_mod.parse(r.order_date)
        recipient = recipient_by_id.get(r.order_id)
        if r.status == "success" and r.tracking_no:
            report.success(r.order_id, r.courier, r.tracking_no,
                           order_date=ordered_on)
            upload_rows.append((r.order_id, r.tracking_no, r.courier))
            log(f"  {r.order_id}: 성공 ({r.courier} / {r.tracking_no})")
        elif r.status == "not_yet":
            report.skip(r.order_id, r.reason or "아직 송장번호 미발급",
                        recipient_name=recipient, order_date=ordered_on)
            log(f"  {r.order_id}: 아직 송장번호 미발급 - 건너뜀")
        elif r.status == "cancelled":
            # 기다려도 송장이 안 나오는 주문이라 일반 스킵과 분리한다
            # (suppliers/base.py의 OrderCancelled와 같은 취급).
            reason = f"주문 화면에 취소/품절 표시가 있습니다: {r.reason or '(문구 없음)'}"
            report.cancelled(r.order_id, reason, recipient_name=recipient,
                             order_date=ordered_on)
            log(f"  {r.order_id}: 취소/품절로 보임 - 건너뜀")
        else:
            reason = r.reason or "알 수 없는 오류"
            report.fail(r.order_id, reason, recipient_name=recipient,
                        order_date=ordered_on)
            log(f"  {r.order_id}: 실패 ({reason})")

    # orchestrator와 같은 규칙으로 '한 주문번호가 여러 행' 주문을 뺀다.
    # CJ온스타일 건은 이 경로로만 업로드 파일에 들어가므로 여기서도 걸러야 한다.
    upload_rows, dropped = excel_io.resolve_duplicate_orders(all_orders, upload_rows)
    for order_id in dropped:
        report.exclude(order_id, excel_io.SPLIT_ORDER_REASON,
                       recipient_name=recipient_by_id.get(order_id))
        log(f"  {order_id}: 여러 행으로 나뉜 주문이라 자동 반영에서 제외 - 직접 처리해주세요")

    if upload_rows:
        excel_io.append_upload_rows(upload_rows, output_path)
    return len(upload_rows)
