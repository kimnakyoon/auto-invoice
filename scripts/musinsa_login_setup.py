"""무신사 3개 계정 최초 로그인 세팅.

쿠키 이식(import_chrome_session.py) 방식이 무신사에서는 Cloudflare 때문에
통하지 않아(다른 브라우저로 쿠키만 옮기면 세션이 무효 처리됨), 롯데온/
지마켓/네이버와 동일하게 이 스크립트가 직접 띄우는 브라우저 창에서 최초
1회 사람이 로그인하는 방식을 쓴다.

실행:
    python scripts/musinsa_login_setup.py

브라우저 창이 뜨면 아이디는 자동 입력되어 있으니 비밀번호만 입력하고
로그인하면 된다. 로그인이 확인되면 자동으로 다음 계정으로 넘어간다.
3개 계정 전부 끝나면 창이 자동으로 닫힌다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import musinsa  # noqa: E402

load_dotenv()

# 마이페이지(/mypage)는 비로그인 상태에서도 200으로 렌더링되어(게스트 화면) 로그인
# 여부를 URL로 판별할 수 없다 - 반드시 비로그인 시 로그인 페이지로 리다이렉트되는
# 보호된 페이지(주문상세)를 써야 musinsa._looks_like_login_page()가 제대로 감지한다.
CHECK_URL = "https://www.musinsa.com/order/order-detail/202608250706390001"

ACCOUNTS = [
    ("1", musinsa.SITE_KEY, "MUSINSA_ID"),
    ("2", musinsa.SECOND_ACCOUNT_STATE_KEY, "MUSINSA_ID2"),
    ("3", musinsa.THIRD_ACCOUNT_STATE_KEY, "MUSINSA_ID3"),
]


def _goto(page, url: str) -> None:
    """로그인 안 된 상태에서 보호된 페이지로 이동하면 로드 도중 로그인
    페이지로 클라이언트사이드 리다이렉트가 걸려 Playwright가 "다른 탐색에
    의해 중단됨" 오류를 던지는 경우가 있다 - 리다이렉트 자체는 정상 동작이니
    무시하고 page.url로 실제 도착 위치만 확인하면 된다."""
    try:
        page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError:
        page.wait_for_timeout(1500)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            for label, state_key, id_env in ACCOUNTS:
                state_path = browser_mod.state_path(state_key)
                context = (
                    browser.new_context(storage_state=str(state_path))
                    if state_path.exists()
                    else browser.new_context()
                )
                page = context.new_page()
                _goto(page, CHECK_URL)

                if musinsa._looks_like_login_page(page):
                    print(f"[계정{label}] 로그인이 필요합니다. 브라우저 창에서 비밀번호를 입력하고 로그인해주세요.")
                    musinsa._prefill_login_id(page, os.environ.get(id_env))
                    if not musinsa._wait_for_manual_login(page):
                        print(f"[계정{label}] 로그인 대기 시간(5분)이 지났습니다. 다시 실행해주세요.")
                        context.close()
                        continue
                    _goto(page, CHECK_URL)

                context.storage_state(path=str(state_path))
                print(f"[계정{label}] 로그인 세션 저장 완료: {state_path}")
                context.close()
        finally:
            browser.close()

    print("\n모든 계정 처리 완료.")


if __name__ == "__main__":
    main()
