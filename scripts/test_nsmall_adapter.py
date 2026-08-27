"""1단계 검증: NS홈쇼핑 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_nsmall_adapter.py

NSMALL_ID/NSMALL_PW로 완전 자동 로그인하므로 사람이 개입할 필요가 없다.
로그인 세션은 auth/nsmall_state.json 에 저장되어 다음 실행부터는 쿠키로
바로 조회된다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import nsmall  # noqa: E402
from auto_invoice.suppliers.base import TrackingNotAvailableYet  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
TEST_CASES = [
    (
        "https://m.nsmall.com/cs/order-detail?orderNum=560810004712",
        "316977481774",
        "롯데택배",
    ),
]

# 아직 발송되지 않은("상품 준비 중") 주문 - TrackingNotAvailableYet으로 정상
# 스킵되는지만 확인한다 (송장번호가 없으므로 값 비교는 하지 않는다).
NOT_YET_SHIPPED_CASES = [
    "https://m.nsmall.com/cs/order-detail?orderNum=560824001998",
]


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, nsmall.SITE_KEY, headless=False)
        try:
            for product_url, expected_tracking_no, expected_courier in TEST_CASES:
                result = nsmall.get_tracking(context, product_url, headless=False)
                print(f"--- {product_url}")
                print("송장번호:", result.tracking_no)
                print("택배사:", result.courier)
                ok = result.tracking_no == expected_tracking_no and result.courier == expected_courier
                if ok:
                    print("✅ 예상했던 값과 일치합니다.")
                else:
                    print(f"⚠️ 예상값(송장:{expected_tracking_no}, 택배사:{expected_courier})과 다릅니다.")

            for product_url in NOT_YET_SHIPPED_CASES:
                print(f"--- {product_url} (미발송 예상)")
                try:
                    result = nsmall.get_tracking(context, product_url, headless=False)
                    print(f"⚠️ 미발송 주문일 것으로 예상했는데 조회됨: {result}")
                except TrackingNotAvailableYet as e:
                    print(f"✅ 예상대로 미발송으로 스킵됨: {e}")
        finally:
            browser_mod.save_state(context, nsmall.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
