"""공급사 어댑터 공통 인터페이스와 예외 정의.

새 공급사를 추가할 때는 이 파일의 예외 타입을 사용하고,
_template.py를 복사해서 시작하면 된다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Callable, Iterable, Protocol

from playwright.sync_api import BrowserContext

from .. import eta as eta_mod
from .. import order_date as order_date_mod
from ..models import TrackingResult

_OPTION_NOISE_PATTERN = re.compile(r"[\s/|·,\-_()]+")


def normalize_option(text: str | None) -> str:
    """주문옵션 문자열을 느슨하게 비교하기 위한 정규화.

    "(59)Navy · 95(095)" 같은 화면 표기와 샵마인 엑셀의 "주문옵션" 값이 공백/
    구분자 표기만 다르고 내용은 같은 경우가 많아, 그런 잡음을 다 지우고
    소문자로 맞춰서 비교한다.
    """
    return _OPTION_NOISE_PATTERN.sub("", text or "").lower()


class AdapterError(Exception):
    """공급사 어댑터 관련 기본 예외.

    order_date: 주문상세에서 읽은 주문일. 송장이 안 나온 주문(미발급/취소)
    일수록 '주문한 지 며칠 됐는지'가 중요해서, 성공했을 때만이 아니라 예외로
    끝났을 때도 결과 정리까지 값을 실어 보낸다. 못 읽었으면 None이다.

    delivery_note: 같은 화면에서 읽은 '출고예정 2026-09-02' 같은 안내 문구
    (eta.py). 주문일이 오래됐는데 아직 송장이 없는 건을 사람이 볼 때, 공급사가
    이미 예정일을 알려주고 있는지가 판단의 절반이라 같이 실어 보낸다.

    sent_request: 이 결론을 내리기까지 공급사 사이트에 요청을 보냈는가.
    미리 읽어둔 주문내역 목록만으로 답한 경우(롯데온 prepare_batch)만 False다.
    오케스트레이터가 요청 사이 간격(1.5~4초)을 지킬지 판단하는 데 쓴다 -
    요청을 안 보냈으면 간격을 둘 이유도 없다.
    """

    order_date: date | None = None
    delivery_note: str | None = None
    sent_request: bool = True


class OrderNotFound(AdapterError):
    """공급사 사이트에 해당 주문번호가 존재하지 않음."""


class TrackingNotAvailableYet(AdapterError):
    """아직 송장번호가 발급되지 않음 (정상적인 스킵 사유이지 오류가 아님).

    skip_reason: 결과 정리의 '사유' 칸에 적는 짧은 말. 예외 문구(str(e))는
    로그용으로 길고, 결과 엑셀에는 같은 종류끼리 같은 말로 묶여야 사람이
    한눈에 세기 좋다.
    """

    skip_reason: str = "아직 송장번호 미발급"


class ShipmentDelayed(TrackingNotAvailableYet):
    """공급사가 '발송지연/배송지연/출고지연'을 알린 주문.

    기다리면 송장이 나오는 건이라 미발급과 같은 스킵이다 - 결과 정리에서
    사유만 '발송지연'으로 구분해 보여준다 (2026-09-05 네이버 실측 전에는
    '배송조회 버튼을 찾지 못했습니다' 실패로 올라갔다).
    """

    skip_reason = "발송지연 (송장 미발급)"


class ParseError(AdapterError):
    """응답 구조를 해석할 수 없음 (사이트 구조가 바뀌었을 가능성)."""


class BlockedError(AdapterError):
    """봇 차단(Imperva 등) 또는 로그인이 필요한 상태가 감지됨."""


class OrderCancelled(AdapterError):
    """취소되었거나 품절이라 송장번호가 나올 수 없는 주문.

    TrackingNotAvailableYet("아직 미발급")과 반드시 구분해야 한다 - 저쪽은
    기다리면 해결되지만 이쪽은 아무리 기다려도 송장이 안 나와서, 섞어두면
    같은 주문이 매 실행마다 조용히 스킵되며 영영 남는다. 따로 모아서 사람이
    샵마인에서 직접 처리하도록 안내한다.
    """


# 사용자가 지정한 판별 단어. 화면(또는 주문상태) 텍스트에 이 중 하나라도
# 있으면 취소/품절 주문으로 본다.
CANCELLED_KEYWORDS = ("취소", "품절", "불가")

# 단, 이 단어가 같이 있으면 그쪽을 **먼저** 본다. "배송준비중" / "상품 준비중"
# 처럼 아직 진행 중이라는 표시가 함께 있는 화면에서 눈에 띄는 '취소'는 대개
# [주문취소] 버튼이나 '취소 불가' 안내 같은 화면 부속이지 이 주문의 상태가
# 아니다. 그런 주문은 기다리면 송장이 나오므로 취소/품절이 아니라 '아직
# 미발급'으로 넘긴다 - 취소로 잘못 분류하면 사람이 직접 처리해야 할 목록에
# 올라가는 데다, 정작 다음 실행에서 다시 조회되지도 않는다.
PREPARING_KEYWORD = "준비"

# 공급사가 지연을 알리는 표기. '지연' 한 글자만으로 잡으면 화면의 "배송이
# 지연될 수 있습니다" 같은 안내문에도 걸리므로 '발송지연 / 배송 지연 / 출고지연 /
# 지연발송' 꼴만 본다 (실측한 상태값 '발송지연'도 이 꼴이다).
DELAY_PATTERN = re.compile(r"(?:발송|배송|출고|입고|출하)\s*지연|지연\s*(?:발송|배송|출고)")


def find_cancelled_keyword(text: str | None) -> str | None:
    for keyword in CANCELLED_KEYWORDS:
        if keyword in (text or ""):
            return keyword
    return None


def find_preparing_keyword(text: str | None) -> str | None:
    """'준비' 표시 (없으면 None). '상품 준비중'처럼 띄어 쓴 것도 잡는다."""
    squashed = re.sub(r"\s+", "", text or "")
    return PREPARING_KEYWORD if PREPARING_KEYWORD in squashed else None


def find_delay_keyword(text: str | None) -> str | None:
    """'발송지연' 같은 지연 표기 (없으면 None). 띄어 쓴 것도 잡는다."""
    m = DELAY_PATTERN.search(text or "")
    return re.sub(r"\s+", "", m.group(0)) if m else None


def raise_if_delayed(text: str | None, order_no: str) -> None:
    """지연 표기가 있으면 ShipmentDelayed를 던진다.

    raise_if_cancelled가 먼저 부르므로 어댑터가 따로 부를 일은 드물다 - 송장
    유무를 보기 전에 raise_if_cancelled_any를 부르는 곳(지마켓/네이버 API처럼
    상태가 여러 줄인 곳)만 송장이 없음을 확인한 뒤 raise_if_delayed_any를 쓴다.
    """
    keyword = find_delay_keyword(text)
    if keyword:
        raise ShipmentDelayed(
            f"공급사가 '{keyword}'을(를) 알린 주문이라 아직 발송 전으로 봅니다 (주문번호={order_no}).")


def raise_if_delayed_any(texts: Iterable[str | None], order_no: str) -> None:
    """주문상태 값이 여러 줄인 곳용 - 한 줄이라도 지연이면 던진다."""
    raise_if_delayed(" / ".join(t for t in texts if t), order_no)


def raise_if_cancelled(text: str | None, order_no: str, *, delayed: bool = True) -> None:
    """취소/품절 표시가 있으면 OrderCancelled를 던진다.

    지연 표기('발송지연' 등)가 있으면 그쪽을 먼저 본다(ShipmentDelayed) - 지연
    주문은 준비중 문구가 없어 그대로 두면 파싱 실패로 떨어진다. delayed=False면
    이 판정을 건너뛴다 (raise_if_cancelled_any가 쓴다 - 아래 참고).

    '준비'가 함께 있으면 그쪽이 이긴다 - 취소가 아니라 TrackingNotAvailableYet
    ('아직 미발급')으로 넘어가서, 다음 실행에서 다시 조회된다.

    주의: 주문상태 필드가 아니라 **화면 전체 텍스트**를 넘기면 "주문취소"
    버튼 이름 같은 것에도 걸린다. 그래서 호출 규칙을 둘로 정해뒀다:

      - 주문상태 문자열을 정확히 읽을 수 있는 공급사(옥션/GS샵/무신사)는
        그 상태값만 넘기고, NOT_YET 판정보다 **먼저** 부른다.
      - 화면 전체 텍스트밖에 없는 공급사는 NOT_YET 판정 **뒤에**, 즉 송장도
        못 찾고 진행중 상태 문구도 없는 실패 경로에서만 부른다. 여기까지 온
        주문은 어차피 사람이 봐야 하는 건이라, 사유가 "파싱 실패" 대신
        "취소/품절 의심"으로 조금 넓게 잡혀도 손해가 없다.

    한 주문이 여러 줄(옵션별)로 나뉜 화면은 raise_if_cancelled_any 를 쓴다.
    """
    if delayed:
        raise_if_delayed(text, order_no)
    keyword = find_cancelled_keyword(text)
    if not keyword:
        return
    if find_preparing_keyword(text):
        raise TrackingNotAvailableYet(
            f"'{keyword}' 표시가 있지만 '{PREPARING_KEYWORD}' 표시가 함께 있어 "
            f"아직 준비 중인 주문으로 봅니다 (주문번호={order_no})."
        )
    raise OrderCancelled(
        f"주문 화면에 '{keyword}' 표시가 있습니다 (주문번호={order_no}) - "
        "취소/품절 주문인지 확인해주세요."
    )


def raise_if_cancelled_any(texts: Iterable[str | None], order_no: str) -> None:
    """상태값이 여러 줄인 화면용. 한 줄이라도 '준비'면 그쪽이 이긴다.

    한 주문이 옵션별로 여러 줄로 나뉘면 줄마다 상태가 다를 수 있다. 줄 하나씩
    raise_if_cancelled 에 넘기면 '취소' 줄을 먼저 만나는 순간 거기서 끝나버려
    뒤에 있는 '배송준비중' 줄을 못 본다. 그래서 준비 여부는 줄을 다 모아서
    먼저 본다.

    지연은 여기서 보지 않는다. 여러 줄 화면은 대개 송장 유무를 보기 **전에**
    이 함수를 부르는데(지마켓/네이버 API), 한 줄이 지연이고 다른 줄은 이미
    나갔으면 그 송장을 써야지 지연으로 넘기면 안 된다. 호출한 쪽이 송장이
    없음을 확인한 뒤 raise_if_delayed_any를 따로 부른다.
    """
    values = [t for t in texts if t]
    joined = " / ".join(values)
    keyword = find_cancelled_keyword(joined)
    if keyword and find_preparing_keyword(joined):
        raise TrackingNotAvailableYet(
            f"'{keyword}' 줄이 있지만 '{PREPARING_KEYWORD}' 줄이 함께 있어 "
            f"아직 준비 중인 주문으로 봅니다 (주문번호={order_no}, 상태={joined})."
        )
    for value in values:
        raise_if_cancelled(value, order_no, delayed=False)


def with_order_date(page, fetch: Callable[[], TrackingResult], *, data=None) -> TrackingResult:
    """주문상세 화면에서 주문일과 출고/도착 예정 문구를 읽어 결과에 실어준다.

    각 어댑터의 get_tracking에서 '주문상세 화면에 도착한 직후' 이 함수로
    실제 조회를 감싸기만 하면 된다 - 화면을 떠나기 전에 날짜부터 읽어두고,
    조회가 예외로 끝나도(아직 미발급/취소 등) 그 예외에 날짜를 붙여준다.

    화면 텍스트는 한 번만 읽어 주문일(order_date.py)과 예정 문구(eta.py)에
    같이 쓴다 - 둘 다 같은 화면 같은 자리에서 나오는 값이라 두 번 읽을 이유가
    없다.

    data: 주문 정보 JSON(화면에 심어둔 entry-data 등)이 있으면 화면 텍스트
    보다 먼저 본다 - 라벨을 찾아 헤매지 않아도 되니 더 정확하다.
    """
    text = _page_text(page)
    found = order_date_mod.from_json(data) if data is not None else None
    return attach_order_date(found or order_date_mod.from_text(text), fetch,
                             delivery_note=eta_mod.from_text(text))


def _page_text(page) -> str:
    """주문상세 화면의 본문 텍스트. 못 읽어도 조회 자체를 깨지 않는다."""
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001 - 부가 정보 때문에 조회가 깨지면 안 된다
        return ""


def attach_order_date(found: date | None, fetch: Callable[[], TrackingResult],
                      *, delivery_note: str | None = None) -> TrackingResult:
    """이미 읽어둔 주문일(과 예정 문구)을 조회 결과(또는 예외)에 붙인다.

    화면이 아니라 API 응답에서 주문일을 얻는 어댑터(무신사/옥션)용. 그쪽은
    주문상세 화면을 아예 열지 않아서 예정 문구가 없고, 그러면 결과 엑셀의
    '출고/도착예정' 칸이 빈칸으로 남는다 - 없는 값을 지어내지는 않는다.

    어댑터가 스스로 채워 넣은 값이 있으면 그쪽을 존중한다.
    """
    try:
        result = fetch()
    except AdapterError as e:
        if e.order_date is None:
            e.order_date = found
        if e.delivery_note is None:
            e.delivery_note = delivery_note
        raise
    if result.order_date is None:
        result.order_date = found
    if result.delivery_note is None:
        result.delivery_note = delivery_note
    return result


class SupplierAdapter(Protocol):
    """각 공급사 모듈이 구현해야 하는 형태.

    보통은 상품URL에 공급사 주문번호가 들어있어서 그것만으로 주문을 특정할 수
    있다. 옥션처럼 상품URL이 주문별 주소가 아니라 목록 페이지 주소인 공급사는
    수령인 이름까지 봐야 어느 주문인지 확정할 수 있는데, 그런 어댑터는
    모듈에 `WANTS_RECIPIENT_NAME = True`를 선언하고 get_tracking에
    `recipient_name` 인자를 추가로 받으면 된다 (orchestrator가 그 표시를 보고
    넘겨준다). 선언하지 않은 어댑터는 아무것도 바꿀 필요가 없다.
    """

    DOMAINS: set[str]
    SITE_KEY: str

    def get_tracking(
        self,
        context: BrowserContext,
        product_url: str,
        headless: bool = True,
        order_option: str | None = None,
    ) -> TrackingResult: ...
