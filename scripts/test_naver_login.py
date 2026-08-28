"""네이버페이 자동 로그인만 따로 검증한다 (계정 2개).

auth/naver_state.json / auth/naver2_state.json(저장된 로그인 세션)을 **쓰지
않고** 빈 브라우저로 시작하기 때문에, 반드시 로그인 경로를 타게 된다. .env의
NAVER_ID/NAVER_PW(+ID2/PW2)로 자동 로그인이 실제로 되는지 확인하는 용도다.

이 사이트는 평소 쓰는 브라우저로 로그인하면 "보안을 위해 추가 확인" 캡차가
떠서, 로그인하는 동안에만 크롬 창이 따로 떴다가 닫힌다 - 크롬을 우리가 직접
실행해 CDP로 붙었을 때만 캡차 없이 통과되기 때문이다(자세한 이유는
suppliers/naver.py docstring). 사람이 타이핑하거나 체크박스를 누를 일은 없다.
조회는 그 창이 아니라 원래 컨텍스트에서 headless로 이어진다.

실행:
    python scripts/test_naver_login.py

성공하면 새로 로그인한 세션을 auth/naver_state.json(+naver2)에 저장한다.
실패하면 기존 세션 파일은 그대로 둔다(멀쩡한 세션을 망가뜨리지 않기 위해).
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import naver  # noqa: E402
from auto_invoice.suppliers.base import OrderCancelled, TrackingNotAvailableYet  # noqa: E402

# test_naver_adapter.py와 같은 주문 - 로그인이 끝난 뒤 실제 조회까지 되는지 본다.
# 1번 계정 소유(배송중), 2번 계정 소유(취소완료 - 송장은 없지만 로그인은 타야 한다).
ACCOUNT1_ORDER_URL = "https://orders.pay.naver.com/order/status/2026082227166731?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"
ACCOUNT2_ORDER_URL = "https://orders.pay.naver.com/order/status/2026082523369571?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"


def main() -> None:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    for env_name in ("NAVER_PW", "NAVER_PW2"):
        if not os.environ.get(env_name):
            print(f"⚠️ .env에 {env_name}가 비어 있습니다. 그 계정은 자동 로그인 대신 수동 로그인 창이 뜹니다.")

    with sync_playwright() as p:
        # 자동 로그인이 띄우는 크롬 창은 이 인스턴스로 만들어진다.
        browser_mod.remember_playwright(p)
        # 조회 쪽은 평소 실행과 같은 조건(번들 Chromium, headless)으로 둔다 -
        # 로그인 창에서 받은 쿠키가 여기로 제대로 옮겨오는지까지 봐야 하기 때문이다.
        browser = p.chromium.launch(headless=True)
        # 저장된 세션을 일부러 쓰지 않는다 - 로그인 경로를 반드시 타게 하려고.
        context = browser.new_context()
        try:
            print("[계정1] 로그인 + 조회 시도...")
            result = naver.get_tracking(context, ACCOUNT1_ORDER_URL, headless=True)
            print("✅ [계정1] 성공 - 송장번호:", result.tracking_no, "/ 택배사:", result.courier)

            # 2번 계정은 1번 계정에 없는 주문이라, 어댑터가 계정을 바꿔가며
            # 두 번째 계정으로도 로그인하게 된다.
            print()
            print("[계정2] 로그인 + 조회 시도...")
            try:
                result2 = naver.get_tracking(context, ACCOUNT2_ORDER_URL, headless=True)
                print("✅ [계정2] 성공 - 송장번호:", result2.tracking_no, "/ 택배사:", result2.courier)
            except (OrderCancelled, TrackingNotAvailableYet) as e:
                # 로그인과 주문 접근까지는 됐다는 뜻이다 (송장이 없는 주문일 뿐).
                print(f"✅ [계정2] 로그인/조회까지 정상 - 송장은 없는 주문입니다: {type(e).__name__}: {e}")

            browser_mod.save_state(context, naver.SITE_KEY)
            print()
            print("   새 로그인 세션을 auth/naver_state.json에 저장했습니다.")
            print("   (2번 계정 세션은 어댑터가 auth/naver2_state.json에 직접 저장합니다.)")
        except Exception as e:
            print(f"❌ 실패: {type(e).__name__}: {e}")
            print("   (기존 세션 파일은 건드리지 않았습니다)")
            # 어디서 어긋났는지 사람이 확인할 수 있도록 마지막 화면을 남긴다.
            shot = Path(__file__).resolve().parent.parent / "logs" / "naver_login_실패화면.png"
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
