"""Playwright 브라우저/컨텍스트 수명주기 관리.

사이트(샵마인, 롯데온, ...)별로 별도의 BrowserContext를 사용하고,
로그인 세션을 storage_state로 저장/재사용해 매 실행마다 다시 로그인하지
않도록 한다. (재로그인이 잦으면 봇 탐지에도 불리하다.)
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Playwright

AUTH_DIR = Path("auth")


def state_path(site_key: str) -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR / f"{site_key}_state.json"


def get_context(playwright: Playwright, site_key: str, headless: bool = True) -> tuple[Browser, BrowserContext]:
    browser = playwright.chromium.launch(headless=headless)
    sp = state_path(site_key)
    if sp.exists():
        context = browser.new_context(storage_state=str(sp))
    else:
        context = browser.new_context()
    return browser, context


def save_state(context: BrowserContext, site_key: str) -> None:
    context.storage_state(path=str(state_path(site_key)))
