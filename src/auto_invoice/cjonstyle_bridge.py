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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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
                    "status": {"type": "string", "enum": ["success", "not_yet", "fail"]},
                    "tracking_no": {"type": "string"},
                    "courier": {"type": "string"},
                    "reason": {"type": "string"},
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
2. 그 결과에서 URL이 로그인 페이지이거나 "/p/myzone/" 경로를 벗어나 홈으로 리다이렉트됐으면 status="fail", reason="로그인 필요"로 기록하고 다음 주문으로 넘어간다.
3. "배송조회" 버튼이 없고 "상품준비중"/"배송준비중"/"결제완료"/"주문접수" 같은 문구가 있으면 status="not_yet"으로 기록하고 다음 주문으로 넘어간다.
4. "배송조회" 버튼이 있으면(여러 개면 order_option과 가장 관련있는 상품 옆의 것을 find로 찾아서) browser_batch 한 번으로 [computer(click), computer(wait 1.5초), get_page_text]를 순서대로 실행한다. 클릭하면 같은 탭에서 deliveryTracking/sheet 페이지로 이동한다.
5. 그 get_page_text 결과에서 "송장번호" 다음 줄의 숫자, "택배업체" 다음 줄의 첫 단어(택배사명)를 읽는다.
6. 택배사명 정규화: "대한통운" 또는 "CJ"가 포함되면 "CJ대한통운", "롯데"가 포함되면 "롯데택배", "DELIBOX"가 포함되면 "딜리박스"로 바꾼다. 매칭 안되면 원문 그대로 쓴다.
7. status="success", tracking_no, courier를 기록한다.

중요: 모든 주문 처리가 끝나면(실패했거나 중간에 문제가 생긴 경우에도 반드시) 이 작업에서 사용한 탭을 tabs_close_mcp로 전부 닫아라. 사용자가 실행 후 크롬 창이 남지 않기를 원한다. 결과 JSON을 출력하기 전에 닫아야 한다.

모든 주문 처리 후, 결과를 JSON으로만 출력해라 (설명 텍스트 없이)."""


@dataclass
class CjonstyleLookupResult:
    order_id: str
    status: str  # "success" | "not_yet" | "fail"
    tracking_no: str | None = None
    courier: str | None = None
    reason: str | None = None


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
    )


def lookup_via_chrome(orders: list[PendingOrder]) -> list[CjonstyleLookupResult]:
    """주문마다 별도의 claude -p --chrome 프로세스를 동시에 띄워 조회한다.

    호출자가 이미 CJ온스타일 주문만 걸러서 넘겨야 한다(filter_cjonstyle_orders).
    한 건이 실패해도 나머지 건 처리에는 영향을 주지 않는다. 병렬 실행 중
    크롬 확장 충돌로 실패한 건이 있을 수 있어(위 docstring 참고), 실패한
    건만 마지막에 순차로 한 번 더 시도한다.
    """
    if not orders:
        return []

    workers = min(len(orders), _MAX_WORKERS)
    # 처음 동시에 뜨는 workers개만 시작 시점을 어긋나게 하면 된다(그 뒤 주문들은
    # 앞 프로세스가 끝난 뒤에 시작하므로 자연히 겹치지 않는다).
    staggers = [(i % workers) * _STAGGER_SEC for i in range(len(orders))]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_lookup_one, orders, staggers))

    for i, result in enumerate(results):
        if result.status == "fail":
            results[i] = _lookup_one(orders[i])

    return results
