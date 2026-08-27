"""상품URL의 도메인으로 알맞은 공급사 어댑터를 찾는 레지스트리.

새 공급사를 추가할 때는:
  1. suppliers/<공급사명>.py 를 작성 (_template.py 참고)
  2. 아래에 import + register() 한 줄만 추가

오케스트레이터는 이 registry만 사용하고 공급사별 로직을 전혀 모른다.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import fashionplus, gmarket, gsshop, hmall, lotteimall, lotteon, musinsa, naver, ssg

_ADAPTERS: dict[str, object] = {}


def register(adapter_module) -> None:
    for domain in adapter_module.DOMAINS:
        _ADAPTERS[domain] = adapter_module


register(lotteon)
register(gmarket)
register(ssg)
register(naver)
register(musinsa)
register(fashionplus)
register(gsshop)
register(lotteimall)
register(hmall)
# 새 공급사 추가 예시:
# from . import newsupplier
# register(newsupplier)


def get_adapter(product_url: str):
    netloc = urlparse(product_url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return _ADAPTERS.get(netloc)
