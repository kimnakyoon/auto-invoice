"""주문상세 화면의 '출고예정 / 도착예정' 같은 안내 문구를 뽑는다.

왜 필요한가: 주문한 지 이틀 넘게 지났는데 아직 송장이 안 나온 주문
(order_date.is_stale)은 사람이 공급사에 물어봐야 하나 판단해야 하는데,
그 판단에 필요한 정보가 주문상세 화면에 이미 적혀 있는 경우가 많다
("출고예정일 9월 2일", "도착예정 2026-09-03"). 조회하는 김에 같이 읽어두면
사람이 사이트를 다시 열어보지 않아도 된다.

주문일(order_date.py)과 달리 이 값은 '참고 문구'라, 못 읽으면 그냥 빈칸으로
둔다. 대신 엉뚱한 걸 주워오지 않도록 두 가지를 지킨다.

  1) '출고/발송/배송/도착 + 예정(또는 확률)' 형태의 라벨 둘레만 본다
  2) 그 안에 날짜(또는 'N일 이내' 같은 기간)가 있을 때만 인정한다

날짜가 라벨 앞에 오는 사이트가 있다 (2026-08-31 실측).

  롯데아이몰   "도착예정일: 09/03(목)"      -> 라벨 **뒤**
  CJ온스타일   "9/4(금)이내 도착예정"       -> 라벨 **앞**
  롯데온       "9/4(금) 이내 도착확률 93%"  -> 라벨 **앞**

그래서 뒤를 먼저 보고, 없으면 앞도 본다. 다만 앞은 아무 날짜나 주우면
바로 옆 항목의 주문일을 예정일로 둔갑시키므로, 날짜와 라벨 사이가
'(금)', '이내', '까지' 같은 연결어뿐일 때만 인정한다 (_GAP).

같은 화면에 '적립예정 / 혜택예정 / 환불 예정 금액' 처럼 배송과 무관한 표기가
섞여 있다(CJ온스타일/G마켓/롯데온 실측). 라벨을 '출고·발송·배송·도착·수령·
입고·픽업' 뒤에 붙은 '예정/확률'로만 한정해 그것들을 걸러낸다.

날짜 해석은 주문일과 반대다. 주문일은 미래면 잘못 읽은 것으로 보지만
(order_date._not_future), 예정일은 원래 미래다. 그래서 연도가 없는 표기
('9월 2일')는 올해로 읽되 한참 지난 날짜가 되면 내년으로 본다 - 연말에
1월 출고예정을 보는 경우다.
"""

from __future__ import annotations

import re
from datetime import date

# '예상 도착 예정일', '출고예정', '발송 예정일시' 등을 한 번에 잡는다.
#
# '예정' 말고 '확률'도 같이 본다. 롯데온은 예정일을 "9/3(목) 이내 도착확률 86%"
# 라고 적는다(2026-09-01 실측, 주문목록 카드 15장 중 15장이 이 표기). 이 도구가
# 조회하는 주문의 절반이 롯데온인데, '예정'만 찾던 규칙에서는 그 전부가 빈칸으로
# 남았다 - 2026-09-01 실행에서 롯데온 스킵 33건 중 32건이 그랬다. 사람이 보기에
# 이건 확률이 아니라 도착예정일이라, 엑셀에도 '도착예정'으로 적는다.
_LABEL = re.compile(
    r"(예상\s*)?(출고|발송|배송|도착|수령|입고|픽업)\s*"
    r"(?:(?:완료\s*)?예정(?:일시|일자|일)?|확률)"
)

# 라벨 뒤 이만큼(글자) 안에서만 날짜를 찾는다. 더 넓히면 옆 항목의 날짜까지
# 끌어온다 (order_date._LABEL_WINDOW 와 같은 이유, 다만 예정일은 라벨 바로
# 뒤에 붙는 편이라 조금 더 좁다).
_WINDOW = 30

# 라벨 앞을 볼 때의 범위. 뒤보다 훨씬 좁게 두고, 그나마도 아래 _GAP 를
# 통과해야 인정한다.
_BEFORE_WINDOW = 16

# 라벨 앞 날짜와 라벨 사이에 올 수 있는 것 - 요일 괄호와 '이내/까지' 정도.
# 이걸로 "9/4(금)이내 도착예정"은 통과시키고, "2026-08-30 주문완료 배송예정"
# 처럼 사이에 다른 말이 낀 경우는 걸러낸다.
_GAP = re.compile(r"^\s*(?:\([^)]{1,4}\))?\s*(?:이내|이전|까지|경|쯤|안|전)?\s*$")

_FULL = re.compile(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*일?(?!\d)")

# 날짜 대신 기간/상대표현으로 적어두는 사이트가 있다 ("2~3일 이내 출고예정").
_RELATIVE = re.compile(r"(오늘|내일|모레|\d+\s*[~-]?\s*\d*\s*일\s*(?:이내|내|후|쯤|경))")

# 연도 없는 표기를 올해로 읽었는데 이만큼 넘게 지난 날짜가 되면 내년으로 본다.
_PAST_TOLERANCE_DAYS = 180

# 한 주문에서 이보다 많이 찾으면 앞의 것만 남긴다 (엑셀 한 칸에 들어가야 한다).
_MAX_ITEMS = 3


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolve(year: int | None, month: int, day: int) -> date | None:
    """연도가 없으면 올해로, 그게 한참 지난 날이면 내년으로 읽는다."""
    if year is not None:
        return _build(year, month, day)
    today = date.today()
    found = _build(today.year, month, day)
    if found is None:
        return None
    if (today - found).days > _PAST_TOLERANCE_DAYS:
        return _build(today.year + 1, month, day)
    return found


def _value_in(window: str) -> str | None:
    """라벨 뒤 조각에서 날짜(또는 기간 표현) 하나를 뽑아 문자열로."""
    m = _FULL.search(window)
    if m:
        found = _resolve(int(m[1]), int(m[2]), int(m[3]))
        return f"{found:%Y-%m-%d}" if found else None
    m = _COMPACT.search(window)
    if m:
        found = _resolve(int(m[1]), int(m[2]), int(m[3]))
        return f"{found:%Y-%m-%d}" if found else None
    m = _MONTH_DAY.search(window)
    if m:
        found = _resolve(None, int(m[1]), int(m[2]))
        return f"{found:%Y-%m-%d}" if found else None
    m = _RELATIVE.search(window)
    if m:
        return re.sub(r"\s+", " ", m[1]).strip()
    return None


def _value_before(window: str) -> str | None:
    """라벨 **앞**에 놓인 날짜 ("9/4(금)이내 도착예정" - CJ온스타일).

    창 안에서 라벨에 가장 가까운(맨 뒤) 날짜를 고르고, 그 날짜와 라벨 사이가
    연결어뿐일 때만 인정한다. 그렇지 않으면 옆 항목의 날짜를 예정일로
    잘못 읽게 된다.
    """
    last = None
    for pattern in (_FULL, _COMPACT, _MONTH_DAY):
        for m in pattern.finditer(window):
            if last is None or m.end() > last[1].end():
                last = (pattern, m)
    if last is None:
        return None
    pattern, m = last
    if not _GAP.match(window[m.end():]):
        return None
    if pattern is _MONTH_DAY:
        found = _resolve(None, int(m[1]), int(m[2]))
    else:
        found = _resolve(int(m[1]), int(m[2]), int(m[3]))
    return f"{found:%Y-%m-%d}" if found else None


def _label_text(match: re.Match) -> str:
    """'예상  도착 예정일' -> '도착예정' (엑셀에 넣을 짧은 형태)."""
    return f"{match[2]}예정"


def from_text(text: str | None) -> str | None:
    """화면 텍스트에서 '출고예정 2026-09-02' 형태의 문구를 만들어 돌려준다.

    같은 라벨이 여러 번 나오면(상품이 여러 개인 주문) 서로 다른 값만 모아
    ' / '로 잇는다. 아무것도 못 찾으면 None - 엑셀에는 빈칸으로 들어간다.
    """
    if not text:
        return None
    found: list[str] = []
    for m in _LABEL.finditer(text):
        # 라벨 뒤를 먼저 본다 (대부분의 사이트). 없으면 라벨 앞도 본다.
        value = (_value_in(text[m.end():m.end() + _WINDOW])
                 or _value_before(text[max(0, m.start() - _BEFORE_WINDOW):m.start()]))
        if value is None:
            continue
        item = f"{_label_text(m)} {value}"
        if item not in found:
            found.append(item)
        if len(found) >= _MAX_ITEMS:
            break
    return " / ".join(found) if found else None


def from_page(page) -> str | None:
    """주문상세 화면(Playwright Page)에서 읽는다. 실패하면 조용히 None."""
    try:
        return from_text(page.inner_text("body"))
    except Exception:  # noqa: BLE001 - 참고 문구 때문에 조회가 깨지면 안 된다
        return None
