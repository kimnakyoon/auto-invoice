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

# 어댑터는 없지만 '아직 지원하지 않는 사이트'는 아닌 도메인. cjonstyle_bridge가
# 실제 크롬으로 따로 조회하므로(위 설명 참고), 결과 정리에서 "직접 조회해주세요"
# 목록에 올리면 안 된다.
HANDLED_ELSEWHERE_DOMAINS = {"base.cjonstyle.com"}


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
# register(cjonstyle)  # 위 설명 참고 - Playwright로는 로그인이 막혀 미등록
# 새 공급사 추가 예시:
# from . import newsupplier
# register(newsupplier)


def _domain_of(product_url: str) -> str:
    netloc = urlparse(product_url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def get_adapter(product_url: str):
    return _ADAPTERS.get(_domain_of(product_url))


def is_handled_elsewhere(product_url: str) -> bool:
    """어댑터는 없지만 다른 경로로 조회되는 사이트인지 (HANDLED_ELSEWHERE_DOMAINS)."""
    return _domain_of(product_url) in HANDLED_ELSEWHERE_DOMAINS
