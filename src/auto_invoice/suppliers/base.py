"""공급사 어댑터 공통 인터페이스와 예외 정의.

새 공급사를 추가할 때는 이 파일의 예외 타입을 사용하고,
_template.py를 복사해서 시작하면 된다.
"""

from __future__ import annotations

from typing import Protocol

from playwright.sync_api import BrowserContext

from ..models import TrackingResult


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


class SupplierAdapter(Protocol):
    """각 공급사 모듈이 구현해야 하는 형태."""

    DOMAINS: set[str]
    SITE_KEY: str

    def get_tracking(
        self, context: BrowserContext, product_url: str, headless: bool = True
    ) -> TrackingResult: ...
