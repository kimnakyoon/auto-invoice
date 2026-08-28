"""Playwright 브라우저/컨텍스트 수명주기 관리.

사이트(샵마인, 롯데온, ...)별로 별도의 BrowserContext를 사용하고,
로그인 세션을 storage_state로 저장/재사용해 매 실행마다 다시 로그인하지
않도록 한다. (재로그인이 잦으면 봇 탐지에도 불리하다.)
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Playwright

AUTH_DIR = Path("auth")

# 실행 중인 Playwright 인스턴스. 어댑터는 BrowserContext만 받기 때문에,
# 로그인만 별도의 브라우저로 띄워야 하는 사이트(현대몰)가 인스턴스를 다시
# 구할 방법이 없어서 여기에 담아둔다.
_playwright: Playwright | None = None


def state_path(site_key: str) -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR / f"{site_key}_state.json"


def remember_playwright(playwright: Playwright) -> None:
    """이후 real_chrome_context()가 쓸 Playwright 인스턴스를 기억해둔다.

    get_context()를 거치지 않는 검증 스크립트는 이걸 직접 불러줘야 한다.
    """
    global _playwright
    _playwright = playwright


def get_context(playwright: Playwright, site_key: str, headless: bool = True) -> tuple[Browser, BrowserContext]:
    remember_playwright(playwright)
    browser = playwright.chromium.launch(headless=headless)
    sp = state_path(site_key)
    if sp.exists():
        context = browser.new_context(storage_state=str(sp))
    else:
        context = browser.new_context()
    return browser, context


def save_state(context: BrowserContext, site_key: str) -> None:
    context.storage_state(path=str(state_path(site_key)))


def current_playwright() -> Playwright | None:
    """get_context()가 마지막으로 받은 Playwright 인스턴스."""
    return _playwright


def real_chrome_profile_dir(site_key: str) -> Path:
    return AUTH_DIR / f"chrome_profile_{site_key}"


def real_chrome_context(
    site_key: str, playwright: Playwright | None = None
) -> BrowserContext:
    """설치된 진짜 크롬을, 계속 재사용되는 프로필로 띄운 컨텍스트.

    reCAPTCHA v3는 "번들 Chromium인지"와 "이력이 있는 프로필인지"를 크게
    보기 때문에, 평소처럼 `chromium.launch()` + 빈 컨텍스트로 로그인하면
    점수가 바닥이라 사이트가 로그인을 거부한다(현대몰 실측: 0.4 -> 거부).
    같은 계정/같은 동작이라도 이 함수로 띄우면 0.8이 나와 통과한다.

    창을 반드시 띄운다(headless=False) - 진짜 크롬이라도 headless면 점수가
    0.1로 떨어져 로그인이 거부되는 것을 확인했다(2026-08-28 실측). 그래서
    이 컨텍스트는 로그인에만 쓰고, 조회는 기존 headless 컨텍스트에 쿠키를
    옮겨서 그대로 진행한다.

    프로필은 auth/chrome_profile_<사이트>에 남는다(auth/는 .gitignore 대상).
    쓸수록 이력이 쌓여 점수에 유리하므로 지우지 않는다.
    """
    pw = playwright or _playwright
    if pw is None:
        raise RuntimeError("Playwright 인스턴스가 없습니다 - get_context()를 먼저 호출해야 합니다.")
    profile = real_chrome_profile_dir(site_key)
    profile.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="chrome",
        headless=False,
        viewport={"width": 412, "height": 900},
        locale="ko-KR",
        timezone_id="Asia/Seoul",
    )
