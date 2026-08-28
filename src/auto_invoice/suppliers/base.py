"""공급사 어댑터 공통 인터페이스와 예외 정의.

새 공급사를 추가할 때는 이 파일의 예외 타입을 사용하고,
_template.py를 복사해서 시작하면 된다.
"""

from __future__ import annotations

import re
from typing import Protocol

from playwright.sync_api import BrowserContext

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
    """공급사 어댑터 관련 기본 예외."""


class OrderNotFound(AdapterError):
    """공급사 사이트에 해당 주문번호가 존재하지 않음."""


class TrackingNotAvailableYet(AdapterError):
    """아직 송장번호가 발급되지 않음 (정상적인 스킵 사유이지 오류가 아님)."""


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
CANCELLED_KEYWORDS = ("취소", "품절")


def find_cancelled_keyword(text: str | None) -> str | None:
    for keyword in CANCELLED_KEYWORDS:
        if keyword in (text or ""):
            return keyword
    return None


def raise_if_cancelled(text: str | None, order_no: str) -> None:
    """취소/품절 표시가 있으면 OrderCancelled를 던진다.

    주의: 주문상태 필드가 아니라 **화면 전체 텍스트**를 넘기면 "주문취소"
    버튼 이름 같은 것에도 걸린다. 그래서 호출 규칙을 둘로 정해뒀다:

      - 주문상태 문자열을 정확히 읽을 수 있는 공급사(옥션/GS샵/무신사)는
        그 상태값만 넘기고, NOT_YET 판정보다 **먼저** 부른다.
      - 화면 전체 텍스트밖에 없는 공급사는 NOT_YET 판정 **뒤에**, 즉 송장도
        못 찾고 진행중 상태 문구도 없는 실패 경로에서만 부른다. 여기까지 온
        주문은 어차피 사람이 봐야 하는 건이라, 사유가 "파싱 실패" 대신
        "취소/품절 의심"으로 조금 넓게 잡혀도 손해가 없다.
    """
    keyword = find_cancelled_keyword(text)
    if keyword:
        raise OrderCancelled(
            f"주문 화면에 '{keyword}' 표시가 있습니다 (주문번호={order_no}) - "
            "취소/품절 주문인지 확인해주세요."
        )


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
