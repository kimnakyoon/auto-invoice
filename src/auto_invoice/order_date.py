"""주문상세에 적힌 '주문일'을 읽고, 얼마나 묵은 주문인지 판정한다.

왜 필요한가: 송장조회를 돌렸는데 아직 송장이 없어 넘긴 주문 중에 주문일이
이틀 이상 지난 건은 공급사 쪽 발송이 늦어지고 있다는 뜻이라 사람이 따로
챙겨봐야 한다(예: 주문상세 8월 26일 / 오늘 8월 28일). 그래서 조회하는 김에
주문상세의 날짜를 같이 읽어두고, 마지막 결과 요약과 결과 엑셀에 따로 모아
보여준다. 어떤 건을 그 목록에 넣을지는 report.py의 is_stale_entry가
정한다 - 이 모듈은 날짜만 다룬다.

날짜를 잘못 읽어 멀쩡한 주문을 '오래됨'으로 올리면 목록 전체를 못 믿게 되므로,
화면에 있는 아무 날짜나 줍지 않는다. '주문일자/결제일' 같은 라벨 바로 뒤
40자 안에 붙은 날짜만 주문일로 인정하고, 라벨을 못 찾으면 None으로 둔 채
아무 표시도 하지 않는다 - 잘못 표시하느니 표시하지 않는 쪽이 낫다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# 오늘과 이만큼(일) 이상 벌어진 주문일이면 따로 모아 보여준다.
# 사용자 기준: 주문상세 8월 26일 / 오늘 8월 28일 = 2일 -> 해당됨.
STALE_DAYS = 2

# 주문상세 화면에서 주문일 앞에 붙는 라벨. 앞에 있는 것부터 찾아서 먼저
# 걸리는 쪽을 쓴다("주문일자"가 "주문일"보다 앞에 있어야 하는 이유).
_LABELS = (
    "주문일자", "주문일시", "주문날짜", "주문/결제일", "주문일",
    "결제일자", "결제일시", "결제일",
    "구매일자", "구매일",
    "결제완료일",
)

# 라벨 뒤 이만큼(글자) 안에서만 날짜를 찾는다. 더 넓히면 옆 항목의 날짜
# (배송예정일 등)까지 끌어오게 된다.
_LABEL_WINDOW = 40

_FULL = re.compile(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_SHORT_YEAR = re.compile(r"(?<!\d)(\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?(?!\d)")

# API 응답(JSON)에서 주문일로 볼 키 이름 (대소문자/밑줄을 지우고 비교).
# orderDate, ordDt, orderYmd, paymentDate, orderedAt 같은 표기를 모두 잡는다.
_JSON_KEY = re.compile(
    r"^(order|ord|pay|payment|purchase|buy|reg)(ed)?(date|dt|day|ymd|datetime|dttm|at)$"
)


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None  # 2026-13-40 같은 잘못 읽은 값


def _shift_back_if_future(parsed: date | None) -> date | None:
    """연도 없는 표기(8월 26일)는 올해로 읽되, 미래가 되면 작년으로 본다.

    연말에 12월 주문을 1월에 조회하는 경우다. 하루치는 시차/시계 오차로
    보고 미래로 치지 않는다.
    """
    if parsed is None or parsed <= date.today() + timedelta(days=1):
        return parsed
    return _build(parsed.year - 1, parsed.month, parsed.day)


def parse(text: str | None) -> date | None:
    """짧은 문자열 하나에서 날짜를 읽는다 (라벨 뒤 조각이나 JSON 값 하나)."""
    if not text:
        return None
    m = _FULL.search(text)
    if m:
        return _build(int(m[1]), int(m[2]), int(m[3]))
    m = _COMPACT.search(text)
    if m:
        return _build(int(m[1]), int(m[2]), int(m[3]))
    m = _SHORT_YEAR.search(text)
    if m:
        return _build(2000 + int(m[1]), int(m[2]), int(m[3]))
    m = _MONTH_DAY.search(text)
    if m:
        return _shift_back_if_future(_build(date.today().year, int(m[1]), int(m[2])))
    return None


def from_text(text: str | None) -> date | None:
    """주문상세 화면 텍스트에서 주문일을 읽는다 (라벨 뒤에 붙은 날짜만)."""
    if not text:
        return None
    for label in _LABELS:
        for m in re.finditer(re.escape(label), text):
            found = parse(text[m.end():m.end() + _LABEL_WINDOW])
            if found is not None:
                return found
    return None


def from_page(page) -> date | None:
    """주문상세 화면(Playwright Page)에서 주문일을 읽는다.

    주문일은 있으면 좋은 정보일 뿐이라, 읽다가 무슨 일이 생겨도 송장조회
    자체를 실패시키지 않는다 - 조용히 None으로 둔다.
    """
    try:
        return from_text(page.inner_text("body"))
    except Exception:  # noqa: BLE001 - 주문일 때문에 조회가 깨지면 안 된다
        return None


def from_json(data) -> date | None:
    """API 응답(dict/list)에서 주문일로 보이는 값을 찾는다.

    얕은 곳부터 훑는다(너비 우선) - 주문 단위 정보가 상품 목록 안쪽보다
    바깥에 있어서, 상품별 날짜보다 주문 자체의 날짜가 먼저 걸린다.
    """
    queue = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(value, (str, int)):
                    continue
                if _JSON_KEY.match(str(key).replace("_", "").lower()):
                    found = parse(str(value))
                    if found is not None:
                        return found
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def days_since(order_date: date | None) -> int | None:
    """주문일로부터 오늘까지 며칠 지났는지 (미래 날짜면 음수)."""
    if order_date is None:
        return None
    return (date.today() - order_date).days


def is_stale(order_date: date | None) -> bool:
    days = days_since(order_date)
    return days is not None and days >= STALE_DAYS


def describe(order_date: date | None) -> str:
    """'2026-08-26 (2일 지남)' - 요약과 엑셀에서 같은 문구를 쓴다."""
    if order_date is None:
        return ""
    days = days_since(order_date)
    if days is None or days < 0:
        return f"{order_date:%Y-%m-%d}"
    if days == 0:
        return f"{order_date:%Y-%m-%d} (오늘)"
    return f"{order_date:%Y-%m-%d} ({days}일 지남)"
