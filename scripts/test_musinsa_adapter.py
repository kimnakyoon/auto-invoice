"""1단계 검증: 무신사 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_musinsa_adapter.py

먼저 scripts/import_chrome_session.py로 auth/musinsa_state.json(계정1),
auth/musinsa2_state.json(계정2), auth/musinsa3_state.json(계정3) 중 최소 1개는
만들어져 있어야 한다 (크롬에서 계정을 바꿔가며 스크립트를 최대 3번 실행).
세션이 없으면 이 스크립트를 headless=False로 돌려서 아이디는 자동 입력된
상태에서 비밀번호만 직접 입력해도 된다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import musinsa  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
ORDER1_URL = "https://www.musinsa.com/order/order-detail/202608250706390001"
EXPECTED1_TRACKING_NO = "411603060466"
EXPECTED1_COURIER = "롯데택배"

ORDER2_URL = "https://www.musinsa.com/order/order-detail/202608241312410001"
EXPECTED2_TRACKING_NO = "500011619856"
EXPECTED2_COURIER = "CJ대한통운"


def _check(label: str, result, expected_tracking: str, expected_courier: str) -> None:
    print(f"[{label}] 송장번호:", result.tracking_no)
    print(f"[{label}] 택배사:", result.courier)
    if result.tracking_no == expected_tracking and result.courier == expected_courier:
        print("✅ 예상했던 송장번호/택배사와 일치합니다.")
    else:
        print(
            f"⚠️ 예상값(송장번호={expected_tracking}, 택배사={expected_courier})과 다릅니다. "
            "주문 상태가 바뀌었을 수 있습니다."
        )


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, musinsa.SITE_KEY, headless=True)
        try:
            result1 = musinsa.get_tracking(context, ORDER1_URL, headless=True)
            _check("주문1", result1, EXPECTED1_TRACKING_NO, EXPECTED1_COURIER)

            print()
            result2 = musinsa.get_tracking(context, ORDER2_URL, headless=True)
            _check("주문2", result2, EXPECTED2_TRACKING_NO, EXPECTED2_COURIER)
        finally:
            browser_mod.save_state(context, musinsa.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
