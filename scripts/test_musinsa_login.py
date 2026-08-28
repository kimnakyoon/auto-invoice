"""무신사 자동 로그인만 따로 검증한다 (3계정 전부).

auth/musinsa*_state.json(저장된 로그인 세션)을 **쓰지 않고** 계정마다 빈
브라우저 컨텍스트로 시작하기 때문에, 반드시 로그인 경로를 타게 된다. .env의
MUSINSA_ID/MUSINSA_PW (및 2·3번 계정용)로 자동 로그인이 실제로 되는지, 추가
본인인증이나 봇 확인을 요구받지는 않는지 확인하는 용도다.

실행:
    python scripts/test_musinsa_login.py            # 3계정 전부
    python scripts/test_musinsa_login.py 2          # 2번 계정만

성공한 계정만 새 세션을 auth/musinsa*_state.json에 저장한다. 실패한 계정의
기존 세션 파일은 그대로 둔다(멀쩡한 세션을 망가뜨리지 않기 위해).
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

# 비로그인 상태면 반드시 로그인 페이지로 리다이렉트되는 보호된 페이지여야 한다
# (마이페이지는 게스트 화면이 200으로 떠서 판별에 못 쓴다).
CHECK_URL = musinsa.ORDER_DETAIL_URL.format(order_no="202608250706390001")


def _goto(page, url: str) -> None:
    """로그인 전에는 로드 도중 로그인 페이지로 리다이렉트가 걸려 Playwright가
    "다른 탐색에 의해 중단됨" 오류를 던지는 경우가 있다 - 정상 동작이니 무시하고
    page.url로 실제 도착 위치만 본다 (musinsa_login_setup.py와 같은 이유)."""
    try:
        page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError:
        page.wait_for_timeout(1500)


def _shoot(page, label: str) -> None:
    """어디서 어긋났는지 사람이 확인할 수 있도록 마지막 화면을 남긴다."""
    shot = Path(__file__).resolve().parent.parent / "logs" / f"musinsa{label}_login_실패화면.png"
    shot.parent.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"   마지막 화면을 저장했습니다: {shot}")
    except Exception as e:
        print(f"   (화면 저장 실패: {e})")


def _test_account(browser, label: str) -> bool:
    state_key, id_env, pw_env = musinsa.ACCOUNTS[label]
    print(f"\n=== 계정{label} ({id_env}) ===")
    if not os.environ.get(pw_env):
        print(f"⏭️ .env에 {pw_env}가 비어 있어 건너뜁니다 (자동 로그인 대상이 아님).")
        return True

    # 저장된 세션을 일부러 쓰지 않는다 - 로그인 경로를 반드시 타게 하려고.
    context = browser.new_context()
    page = context.new_page()
    try:
        _goto(page, CHECK_URL)
        if not musinsa._looks_like_login_page(page):
            print("⚠️ 빈 컨텍스트인데도 로그인 페이지가 아닙니다 - 검증할 수 없습니다.")
            return False

        musinsa._auto_login(page, label)
        print("✅ 자동 로그인 성공")

        # 로그인만 되고 조회가 안 되면 의미가 없으니 API까지 한 번 태워본다.
        # 이 주문이 어느 계정 것인지는 모르므로, 남의 계정 주문이면
        # INVALID_DATA가 오는 게 정상이다 - 로그인 여부만 확인한다.
        data = musinsa._fetch_order_view(context, musinsa.extract_order_no(CHECK_URL))
        if data is None:
            print("❌ 로그인 후에도 API가 로그인 페이지를 돌려줍니다.")
            _shoot(page, label)
            return False
        print(f"   주문조회 API 응답: result={data.get('result')} (INVALID_DATA면 이 계정 주문이 아니라는 뜻)")

        context.storage_state(path=str(browser_mod.state_path(state_key)))
        print(f"   새 로그인 세션을 {browser_mod.state_path(state_key)}에 저장했습니다.")
        return True
    except Exception as e:
        print(f"❌ 실패: {type(e).__name__}: {e}")
        print("   (기존 세션 파일은 건드리지 않았습니다)")
        _shoot(page, label)
        return False
    finally:
        context.close()


def main() -> None:
    labels = sys.argv[1:] or list(musinsa.ACCOUNTS)
    unknown = [x for x in labels if x not in musinsa.ACCOUNTS]
    if unknown:
        print(f"모르는 계정번호: {unknown} (가능한 값: {list(musinsa.ACCOUNTS)})")
        sys.exit(1)

    with sync_playwright() as p:
        # 무신사는 headless로도 로그인 페이지가 뜨지만, 실패했을 때 사람이 바로
        # 이어받아 로그인할 수 있도록 이 검증 스크립트는 창을 띄운다.
        browser = p.chromium.launch(headless=False)
        try:
            results = {label: _test_account(browser, label) for label in labels}
        finally:
            browser.close()

    failed = [label for label, ok in results.items() if not ok]
    print("\n=== 결과 ===")
    if failed:
        print(f"❌ 실패한 계정: {', '.join(failed)}")
        sys.exit(1)
    print("✅ 모든 계정 통과")


if __name__ == "__main__":
    main()
