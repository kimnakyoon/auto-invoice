"""새 공급사 어댑터 작성 템플릿.

사용법:
  1. 이 파일을 suppliers/<공급사명>.py 로 복사
  2. DOMAINS, SITE_KEY를 채운다
  3. get_tracking()을 구현한다 (필요하면 이 사이트도 롯데온과 같은 방식으로
     Network 탭 HAR 캡처를 먼저 해서 실제 API를 찾아볼 것 — lotteon.py 참고)
  4. registry.py에 아래 두 줄을 추가한다
         from . import <공급사명>
         register(<공급사명>)

이 파일 자체는 registry.py에 등록되어 있지 않으므로 그대로 두어도 동작에
영향을 주지 않는다.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext

from ..models import TrackingResult
from .base import AdapterError, BlockedError, OrderNotFound, ParseError, TrackingNotAvailableYet  # noqa: F401

DOMAINS: set[str] = set()  # 예: {"example.com", "www.example.com"}
SITE_KEY = "TODO"


def extract_order_id(product_url: str) -> str:
    raise NotImplementedError


def get_tracking(context: BrowserContext, product_url: str, headless: bool = True) -> TrackingResult:
    raise NotImplementedError
