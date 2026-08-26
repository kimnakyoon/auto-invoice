"""1단계 검증: SSG 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_ssg_adapter.py

SSG는 롯데온/지마켓과 달리 SSG_ID/SSG_PW(.env)로 완전 자동 로그인하므로
브라우저 창에서 직접 로그인할 필요가 없다. 로그인 세션은
auth/ssg_state.json 에 저장되어 다음 실행부터는 재로그인하지 않는다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import ssg  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
TEST_PRODUCT_URL = "https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo=2026082458D816"
EXPECTED_TRACKING_NO = "301774766420"
EXPECTED_COURIER = "CJ대한통운"


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, ssg.SITE_KEY, headless=True)
        try:
            result = ssg.get_tracking(context, TEST_PRODUCT_URL, headless=True)
            print("송장번호:", result.tracking_no)
            print("택배사:", result.courier)
            print("비고:", result.note)
            if result.tracking_no == EXPECTED_TRACKING_NO and result.courier == EXPECTED_COURIER:
                print("✅ 예상했던 송장번호/택배사와 일치합니다.")
            else:
                print(
                    f"⚠️ 예상값(송장번호={EXPECTED_TRACKING_NO}, 택배사={EXPECTED_COURIER})과 다릅니다. "
                    "주문 상태가 바뀌었을 수 있습니다."
                )
        finally:
            browser_mod.save_state(context, ssg.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
