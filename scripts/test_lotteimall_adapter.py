"""1단계 검증: 롯데아이몰 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_lotteimall_adapter.py

최초 실행 시 브라우저 창이 뜨면 직접 롯데아이몰에 로그인하세요. 로그인이 완료되면
자동으로 감지해서 이어서 진행합니다. 로그인 세션은 auth/lotteimall_state.json 에
저장되어 다음 실행부터는 다시 로그인하지 않아도 됩니다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import lotteimall  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
TEST_PRODUCT_URL = "https://www.lotteimall.com/mypage/getOrderDtlInfo.lotte?ord_no=20260824K87597"
EXPECTED_TRACKING_NO = "699776702423"
EXPECTED_COURIER = "CJ대한통운"


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, lotteimall.SITE_KEY, headless=False)
        try:
            result = lotteimall.get_tracking(context, TEST_PRODUCT_URL, headless=False)
            print("송장번호:", result.tracking_no)
            print("택배사:", result.courier)
            print("비고:", result.note)
            ok = result.tracking_no == EXPECTED_TRACKING_NO and result.courier == EXPECTED_COURIER
            if ok:
                print("✅ 예상했던 송장번호/택배사와 일치합니다.")
            else:
                print(f"⚠️ 예상값(송장:{EXPECTED_TRACKING_NO}, 택배사:{EXPECTED_COURIER})과 다릅니다.")
        finally:
            browser_mod.save_state(context, lotteimall.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
