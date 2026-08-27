"""롯데온 자동 로그인만 따로 검증한다.

auth/lotteon_state.json(저장된 로그인 세션)을 **쓰지 않고** 빈 브라우저로
시작하기 때문에, 반드시 로그인 경로를 타게 된다. .env의 LOTTEON_ID/LOTTEON_PW로
자동 로그인이 실제로 되는지, 추가 본인인증을 요구받지는 않는지 확인하는 용도다.

실행:
    python scripts/test_lotteon_login.py

성공하면 새로 로그인한 세션을 auth/lotteon_state.json에 저장한다. 실패하면
기존 세션 파일은 그대로 둔다(멀쩡한 세션을 망가뜨리지 않기 위해).
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import lotteon  # noqa: E402

# test_lotteon_adapter.py와 같은 주문 - 로그인이 끝난 뒤 실제 조회까지 되는지 본다.
TEST_PRODUCT_URL = "https://www.lotteon.com/p/order/claim/orderDetail?odNo=2026082316683630"


def main() -> None:
    import os

    if not os.environ.get("LOTTEON_PW"):
        print("⚠️ .env에 LOTTEON_PW가 비어 있습니다. 자동 로그인 대신 수동 로그인 창이 뜹니다.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        # 저장된 세션을 일부러 쓰지 않는다 - 로그인 경로를 반드시 타게 하려고.
        context = browser.new_context()
        try:
            result = lotteon.get_tracking(context, TEST_PRODUCT_URL, headless=False)
            print("✅ 로그인 + 조회 성공")
            print("   송장번호:", result.tracking_no)
            print("   택배사:", result.courier)
            browser_mod.save_state(context, lotteon.SITE_KEY)
            print("   새 로그인 세션을 auth/lotteon_state.json에 저장했습니다.")
        except Exception as e:
            print(f"❌ 실패: {type(e).__name__}: {e}")
            print("   (기존 세션 파일은 건드리지 않았습니다)")
            # 어디서 어긋났는지 사람이 확인할 수 있도록 마지막 화면을 남긴다.
            # (입력 대기로 멈추지 않는다 - 콘솔 없이 실행될 때 그대로 굳어버린다.)
            shot = Path(__file__).resolve().parent.parent / "logs" / "lotteon_login_실패화면.png"
            shot.parent.mkdir(exist_ok=True)
            try:
                pages = [pg for pg in context.pages if not pg.is_closed()]
                if pages:
                    pages[-1].screenshot(path=str(shot), full_page=True)
                    print(f"   마지막 화면을 저장했습니다: {shot}")
            except Exception as shot_err:
                print(f"   (화면 저장 실패: {shot_err})")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
