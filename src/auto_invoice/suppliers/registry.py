"""상품URL의 도메인으로 알맞은 공급사 어댑터를 찾는 레지스트리.

새 공급사를 추가할 때는:
  1. suppliers/<공급사명>.py 를 작성 (_template.py 참고)
  2. 아래에 import + register() 한 줄만 추가

오케스트레이터는 이 registry만 사용하고 공급사별 로직을 전혀 모른다.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import (
    akplaza,
    auction,
    cjonstyle,
    elevenst,
    fashionplus,
    gmarket,
    gsshop,
    hmall,
    lotteimall,
    lotteon,
    musinsa,
    naver,
    nsmall,
    ssfshop,
    ssg,
    thehyundai,
)

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
register(thehyundai)
register(nsmall)
register(elevenst)
register(auction)
register(akplaza)
register(cjonstyle)
register(ssfshop)
# 새 공급사 추가 예시:
# from . import newsupplier
# register(newsupplier)


def _domain_of(product_url: str) -> str:
    netloc = urlparse(product_url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def get_adapter(product_url: str):
    return _ADAPTERS.get(_domain_of(product_url))
