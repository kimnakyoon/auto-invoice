"""Playwright 브라우저/컨텍스트 수명주기 관리.

사이트(샵마인, 롯데온, ...)별로 별도의 BrowserContext를 사용하고,
로그인 세션을 storage_state로 저장/재사용해 매 실행마다 다시 로그인하지
않도록 한다. (재로그인이 잦으면 봇 탐지에도 불리하다.)
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Playwright

AUTH_DIR = Path("auth")

# 사이트가 모바일 페이지를 쓰면 앞쪽, PC 페이지를 쓰면 뒤쪽을 쓴다.
MOBILE_VIEWPORT = {"width": 412, "height": 900}
DESKTOP_VIEWPORT = {"width": 1280, "height": 900}

# 실행 중인 Playwright 인스턴스. 어댑터는 BrowserContext만 받기 때문에,
# 로그인만 별도의 브라우저로 띄워야 하는 사이트(현대몰)가 인스턴스를 다시
# 구할 방법이 없어서 여기에 담아둔다.
#
# 스레드마다 따로 담는다 - 공급사를 동시에 조회할 때(orchestrator) 스레드마다
# 자기 Playwright를 열고, sync API 객체는 만든 스레드 밖에서 쓰면 안 된다.
# 전역 하나로 두면 나중에 시작한 스레드의 인스턴스를 다른 스레드가 집어간다.
_local = threading.local()


def state_path(site_key: str) -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR / f"{site_key}_state.json"


def remember_playwright(playwright: Playwright) -> None:
    """이후 real_chrome_context()가 쓸 Playwright 인스턴스를 기억해둔다.

    get_context()를 거치지 않는 검증 스크립트는 이걸 직접 불러줘야 한다.
    """
    _local.playwright = playwright


def get_context(playwright: Playwright, site_key: str, headless: bool = True) -> tuple[Browser, BrowserContext]:
    remember_playwright(playwright)
    browser = playwright.chromium.launch(headless=headless)
    sp = state_path(site_key)
    if sp.exists():
        context = browser.new_context(storage_state=str(sp))
    else:
        context = browser.new_context()
    block_heavy_resources(context)
    return browser, context


# 송장번호를 읽는 데 전혀 쓰지 않는 리소스. 쇼핑몰 주문상세는 상품 이미지가
# 대부분의 용량이라, 이것만 받지 않아도 페이지가 훨씬 빨리 뜬다.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# 이미 라우팅을 걸어둔 컨텍스트. 같은 컨텍스트에 두 번 걸면 요청이 두 번
# 가로채여 continue_()가 중복 호출된다.
_ROUTED_CONTEXTS: set[int] = set()


def _abort_heavy(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def block_heavy_resources(context: BrowserContext) -> None:
    """이미지/폰트/동영상을 받지 않게 한다 (조회용 컨텍스트 전체에 적용)."""
    if id(context) in _ROUTED_CONTEXTS:
        return
    context.route("**/*", _abort_heavy)
    _ROUTED_CONTEXTS.add(id(context))


def allow_heavy_resources(context: BrowserContext) -> bool:
    """차단을 푼다. 실제로 풀었으면 True.

    사람에게 화면을 넘길 때(직접 로그인) 부른다 - 캡차가 이미지로 나오는
    사이트(지마켓 등)에서 이미지를 막아둔 채로 넘기면 사람이 읽을 게 없다.
    """
    if id(context) not in _ROUTED_CONTEXTS:
        return False
    with contextlib.suppress(Exception):
        context.unroute("**/*", _abort_heavy)
    _ROUTED_CONTEXTS.discard(id(context))
    return True


def save_state(context: BrowserContext, site_key: str) -> None:
    context.storage_state(path=str(state_path(site_key)))


def current_playwright() -> Playwright | None:
    """이 스레드의 get_context()가 마지막으로 받은 Playwright 인스턴스."""
    return getattr(_local, "playwright", None)


def real_chrome_profile_dir(site_key: str) -> Path:
    return AUTH_DIR / f"chrome_profile_{site_key}"


def real_chrome_context(
    site_key: str,
    playwright: Playwright | None = None,
    viewport: dict[str, int] | None = None,
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

    viewport는 사이트가 모바일 페이지를 쓰는지 PC 페이지를 쓰는지에 맞춰
    호출한 쪽이 정한다 - 기본값(모바일)으로 PC 로그인 페이지를 열면 레이아웃이
    잘려서, 사람이 눌러야 하는 요소가 화면 가장자리에 반쯤 걸치는 일이 있었다.
    """
    pw = playwright or current_playwright()
    if pw is None:
        raise RuntimeError("Playwright 인스턴스가 없습니다 - get_context()를 먼저 호출해야 합니다.")
    profile = real_chrome_profile_dir(site_key)
    profile.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="chrome",
        headless=False,
        viewport=viewport or MOBILE_VIEWPORT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
    )


# 설치된 크롬을 우리가 직접 실행할 때 쓰는 실행파일 후보 경로.
CHROME_PATH_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
# 우리가 띄운 크롬이 디버깅 포트를 열 때까지 기다리는 최대 시간.
CDP_READY_TIMEOUT_SEC = 30


def chrome_executable() -> str:
    for candidate in CHROME_PATH_CANDIDATES:
        path = Path(os.path.expandvars(candidate))
        if path.exists():
            return str(path)
    raise RuntimeError("설치된 크롬(chrome.exe)을 찾지 못했습니다.")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


@contextlib.contextmanager
def real_chrome_cdp_context(site_key: str, playwright: Playwright | None = None):
    """크롬을 **우리가 직접 실행**하고 CDP로 붙은 컨텍스트 (with 문으로 쓴다).

    real_chrome_context()는 진짜 크롬을 쓰긴 해도 Playwright가 자동화 모드로
    실행하기 때문에 navigator.webdriver가 켜져 있고, Cloudflare Turnstile은
    그걸 보고 위젯을 영영 통과시키지 않는다(CJ온스타일 실측: 30초를 기다려도
    cf-turnstile-response 토큰이 빈 값). 같은 크롬이라도 평범한 인자로 우리가
    직접 실행한 뒤 --remote-debugging-port에 connect_over_cdp로 붙으면
    navigator.webdriver가 꺼져 있고 **토큰이 3초 만에 저절로 채워진다**
    (2026-08-28 실측). 사람이 체크박스를 누를 필요도 없다.

    reCAPTCHA v3(현대몰)는 real_chrome_context()로 충분하므로 그쪽은 그대로
    두고, Turnstile이 걸린 사이트만 이 함수를 쓴다.

    프로필은 real_chrome_context()와 같은 auth/chrome_profile_<사이트>를
    쓴다(로그인이 남아 있으면 다음 실행에서 재로그인 없이 넘어간다).
    창은 반드시 보이는 상태로 띄운다 - headless 크롬은 Turnstile/reCAPTCHA
    양쪽 모두에서 점수가 바닥이다.
    """
    pw = playwright or current_playwright()
    if pw is None:
        raise RuntimeError("Playwright 인스턴스가 없습니다 - get_context()를 먼저 호출해야 합니다.")

    profile = real_chrome_profile_dir(site_key)
    profile.mkdir(parents=True, exist_ok=True)
    # --user-data-dir에 상대경로를 주면 크롬이 그 프로필을 쓰지 않고 조용히
    # 종료해버린다(rc=0, 디버깅 포트도 안 연다). real_chrome_context()가 쓰는
    # launch_persistent_context는 Playwright가 절대경로로 바꿔주지만, 여기서는
    # 우리가 직접 실행하므로 직접 절대경로로 만들어야 한다.
    profile = profile.resolve()
    port = _free_port()
    proc = subprocess.Popen(
        [
            chrome_executable(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        # 크롬이 "DevTools listening on ..."을 stderr로 뱉는데, 그대로 두면
        # 실행 로그에 섞여 나온다.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser = None
    try:
        if not _wait_for_port(port, CDP_READY_TIMEOUT_SEC):
            raise RuntimeError(f"크롬이 디버깅 포트({port})를 {CDP_READY_TIMEOUT_SEC}초 안에 열지 않았습니다.")
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        yield context
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
