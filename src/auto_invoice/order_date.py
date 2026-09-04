"""주문상세에 적힌 '주문일'을 읽고, 얼마나 묵은 주문인지 판정한다.

왜 필요한가: 송장조회를 돌렸는데 아직 송장이 없어 넘긴 주문 중에 주문일이
이틀 이상 지난 건은 공급사 쪽 발송이 늦어지고 있다는 뜻이라 사람이 따로
챙겨봐야 한다(예: 주문상세 8월 26일 / 오늘 8월 28일). 그래서 조회하는 김에
주문상세의 날짜를 같이 읽어두고, 마지막 결과 요약과 결과 엑셀에 따로 모아
보여준다. 어떤 건을 그 목록에 넣을지는 report.py의 is_stale_entry가
정한다 - 이 모듈은 날짜만 다룬다. '며칠 지났나'는 주말(토·일)을 빼고 센다
(days_since) - 공급사가 주말에는 출고하지 않으니 금요일 주문을 월요일에 봐도
하루 기다린 셈이다.

날짜를 잘못 읽어 멀쩡한 주문을 '오래됨'으로 올리면 목록 전체를 못 믿게 되므로,
화면에 있는 아무 날짜나 줍지 않는다. 아래 세 규칙 중 하나에 걸리는 날짜만
주문일로 인정하고, 어디에도 안 걸리면 None으로 둔 채 아무 표시도 하지 않는다 -
잘못 표시하느니 표시하지 않는 쪽이 낫다.

  1) 라벨 뒤   '주문일자/결제일/신청일' 같은 라벨 바로 뒤 40자 안의 날짜
  2) 주문번호  '주문번호' 뒤 값이 20260825... / 2026-08-24-K87597 처럼
               날짜로 시작하면 그 앞부분 (11번가/SSG/롯데/CJ온스타일/네이버)
  3) 섹션 제목 '주문 상세', '주문 정보' 같은 제목 바로 뒤 60자 안의 날짜
               (NS홈쇼핑/더현대/Hmall/롯데온/G마켓 - 라벨 없이 날짜만 있다)

2)와 3)은 사이트 11곳의 실제 주문상세 화면을 사람이 직접 확인해서 뽑은
규칙이다(scripts/check_order_date.py로 화면을 띄워 확인했다). 라벨이 붙은
사이트는 패션플러스('신청일') 한 곳뿐이라, 라벨만 찾던 예전 규칙으로는
화면에서 읽는 사이트가 사실상 전부 빈칸이었다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# 오늘과 이만큼(일) 이상 벌어진 주문일이면 따로 모아 보여준다.
# 사용자 기준: 주문상세 8월 26일(수) / 오늘 8월 28일(금) = 2일 -> 해당됨.
# 일수는 주말을 빼고 센다(days_since) - 금요일 주문을 월요일에 보면 1일.
STALE_DAYS = 2

# 주문상세 화면에서 주문일 앞에 붙는 라벨. 앞에 있는 것부터 찾아서 먼저
# 걸리는 쪽을 쓴다("주문일자"가 "주문일"보다 앞에 있어야 하는 이유).
_LABELS = (
    "주문일자", "주문일시", "주문날짜", "주문/결제일", "주문일",
    "결제일자", "결제일시", "결제일",
    "구매일자", "구매일",
    "결제완료일",
    # 패션플러스는 주문일을 '신청일'이라고 부른다.
    "신청일자", "신청일",
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

# 주문번호 앞에 붙는 라벨. 이 뒤의 값이 날짜로 시작하는 공급사가 많다
# (11번가 20260825095273858 / SSG 20260824-58D816 / CJ온스타일 2026-08-27-009262).
_ORDER_NO_LABELS = ("주문번호", "주문 번호")
_ORDER_NO_WINDOW = 30

# 주문번호 값의 맨 앞에 붙은 날짜만 인정한다 (중간에 낀 숫자는 보지 않는다).
_ORDER_NO_DATE = re.compile(r"^(20\d{2})[-.]?(\d{2})[-.]?(\d{2})")

# 라벨 없이 날짜만 있는 사이트를 위한, 날짜가 놓이는 자리의 제목
# ('주문 상세', '주문상세내역', '상세 주문 내역', '주문 정보' 등).
_SECTION = re.compile(r"(?:주문|배송)\s*/?\s*(?:상세|정보|내역)")

# 제목 뒤 이만큼(글자) 안에서만 찾는다. 제목과 날짜 사이에 '주문내역삭제',
# '이용안내' 같은 버튼 글자가 끼는 사이트가 있어 라벨 뒤보다 조금 넓다.
_SECTION_WINDOW = 60


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


def _not_future(found: date | None) -> date | None:
    """주문일이 미래일 수는 없다 - 배송예정일 같은 걸 주웠다는 뜻이라 버린다.

    하루치는 시차/시계 오차로 보고 미래로 치지 않는다(_shift_back_if_future와
    같은 기준).
    """
    if found is None or found > date.today() + timedelta(days=1):
        return None
    return found


def _by_label(text: str) -> date | None:
    """1) '주문일자/신청일' 같은 라벨 바로 뒤에 붙은 날짜."""
    for label in _LABELS:
        for m in re.finditer(re.escape(label), text):
            found = parse(text[m.end():m.end() + _LABEL_WINDOW])
            if found is not None:
                return found
    return None


def _by_order_no(text: str) -> date | None:
    """2) 주문번호 맨 앞에 박힌 날짜.

    주문번호가 날짜로 시작하는 공급사가 많아서 라벨 없이도 주문일을 알 수
    있다. 날짜로 시작하지 않는 주문번호(패션플러스 141262620, 더현대
    260808...)는 _ORDER_NO_DATE에 안 걸리거나 유효한 날짜가 아니라 그냥
    넘어간다.
    """
    for label in _ORDER_NO_LABELS:
        for m in re.finditer(label, text):
            value = text[m.end():m.end() + _ORDER_NO_WINDOW].lstrip(" \t\n:")
            hit = _ORDER_NO_DATE.match(value)
            if hit:
                found = _not_future(_build(int(hit[1]), int(hit[2]), int(hit[3])))
                if found is not None:
                    return found
    return None


def _by_section(text: str) -> date | None:
    """3) '주문 상세' 같은 제목 바로 뒤에 라벨 없이 놓인 날짜."""
    for m in _SECTION.finditer(text):
        found = _not_future(parse(text[m.end():m.end() + _SECTION_WINDOW]))
        if found is not None:
            return found
    return None


def from_text(text: str | None) -> date | None:
    """주문상세 화면 텍스트에서 주문일을 읽는다 (모듈 설명의 세 규칙).

    구체적인 규칙부터 본다. 라벨이 있으면 그게 가장 확실하고, 그다음이
    주문번호에 박힌 날짜다. 제목 뒤 날짜는 셋 중 가장 헐렁해서 맨 뒤에 둔다.
    """
    if not text:
        return None
    for rule in (_by_label, _by_order_no, _by_section):
        found = rule(text)
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


def _weekdays_up_to(day: date) -> int:
    """서력 1년 1월 1일(월요일)부터 이 날까지의 평일(월~금) 수.

    두 날짜의 차이를 내면 그 사이 평일 수가 되므로 하루씩 돌지 않아도 된다.
    ordinal 1이 월요일이라 7로 나눈 나머지가 곧 그 주에서 지난 날 수다.
    """
    weeks, rest = divmod(day.toordinal(), 7)
    return weeks * 5 + min(rest, 5)


def days_since(order_date: date | None, today: date | None = None) -> int | None:
    """주문일로부터 오늘까지 며칠 지났는지 - 주말(토·일)은 빼고 센다.

    주문일 다음 날부터 오늘까지의 평일 수다. 금요일(1일) 주문을 월요일(4일)에
    보면 달력으로는 3일이지만 토·일을 빼고 1일이다(사용자 기준). 주말이 끼지
    않으면 달력 일수와 같다. 토·일에 조회하면 그 날은 세지 않으므로 금요일에
    본 값과 같다 - 공급사가 주말에 출고하지 않는 이상 '더 기다린' 게 아니어서다.
    미래 날짜면 음수. today는 시험용 - 안 주면 오늘이다.
    """
    if order_date is None:
        return None
    return _weekdays_up_to(today or date.today()) - _weekdays_up_to(order_date)


def is_stale(order_date: date | None) -> bool:
    days = days_since(order_date)
    return days is not None and days >= STALE_DAYS


def describe(order_date: date | None) -> str:
    """'2026-08-26 (2일 지남)' - 요약과 엑셀에서 같은 문구를 쓴다 (주말 제외 일수)."""
    if order_date is None:
        return ""
    days = days_since(order_date)
    if days is None or days < 0:
        return f"{order_date:%Y-%m-%d}"
    if days == 0:
        return f"{order_date:%Y-%m-%d} (오늘)"
    return f"{order_date:%Y-%m-%d} ({days}일 지남)"
