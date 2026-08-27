"""상품URL의 도메인으로 알맞은 공급사 어댑터를 찾는 레지스트리.

새 공급사를 추가할 때는:
  1. suppliers/<공급사명>.py 를 작성 (_template.py 참고)
  2. 아래에 import + register() 한 줄만 추가

오케스트레이터는 이 registry만 사용하고 공급사별 로직을 전혀 모른다.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import (
    auction,
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
    ssg,
    thehyundai,
)

# cjonstyle(CJ온스타일)은 어댑터가 작성되어 있으나 여기에 등록하지 않는다:
# 로그인 페이지의 Cloudflare Turnstile("사람인지 확인")이 Playwright로 띄운
# 브라우저를 감지해, 사람이 직접 눌러도 통과하지 못하는 것을 확인했다(실제
# 로그인된 크롬 세션의 쿠키를 옮겨와도 마찬가지였다).
# 대신 이 사이트는 cjonstyle_bridge.py가 claude-in-chrome 확장으로 사용자의
# 실제 크롬 브라우저를 조작해 조회하고(자동화 감지에 걸리지 않는다), GUI가
# 이 배치가 끝난 뒤 그 결과를 같은 업로드 파일에 합친다.
# 나중에 Playwright로도 로그인이 가능해지면(사이트 정책 변경 등) 아래 두 줄의
# 주석만 풀면 이 배치에 포함된다.
# from . import cjonstyle

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
# register(cjonstyle)  # 위 설명 참고 - Playwright로는 로그인이 막혀 미등록
# 새 공급사 추가 예시:
# from . import newsupplier
# register(newsupplier)


def get_adapter(product_url: str):
    netloc = urlparse(product_url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return _ADAPTERS.get(netloc)
