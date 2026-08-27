"""1단계 검증: CJ온스타일 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_cjonstyle_adapter.py

최초 실행 시 브라우저 창이 뜨면 직접 CJ온스타일에 로그인하세요(아이디는 자동
입력됨 - CJONSTYLE_ID). 비밀번호 입력과 "사람인지 확인" 체크박스, 로그인
버튼 클릭은 Cloudflare 봇 차단 때문에 직접 해야 합니다. 로그인이 완료되면
자동으로 감지해서 이어서 진행합니다. 로그인 세션은 auth/cjonstyle_state.json
에 저장되어 다음 실행부터는 다시 로그인하지 않아도 됩니다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import cjonstyle  # noqa: E402
from auto_invoice.suppliers.base import TrackingNotAvailableYet  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
TEST_CASES = [
    (
        "https://base.cjonstyle.com/p/myzone/orderInfo/20260826017435",
        "316726014614",
        "롯데택배",
        None,
    ),
    (
        # 한 주문 안에 환불완료 상품(회수조회)과 배송완료 상품(배송조회)이
        # 함께 있는 경우 - 주문옵션으로 배송완료 상품만 특정되는지 확인.
        "https://base.cjonstyle.com/p/myzone/orderInfo/20260816080967",
        "318626783160",
        "롯데택배",
        "차콜/L",
    ),
]

# 아직 발송되지 않은("상품준비중") 주문 - TrackingNotAvailableYet으로 정상
# 스킵되는지만 확인한다 (송장번호가 없으므로 값 비교는 하지 않는다).
NOT_YET_SHIPPED_CASES = [
    "https://base.cjonstyle.com/p/myzone/orderInfo/20260827009262",
]


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, cjonstyle.SITE_KEY, headless=False)
        try:
            for product_url, expected_tracking_no, expected_courier, order_option in TEST_CASES:
                result = cjonstyle.get_tracking(context, product_url, headless=False, order_option=order_option)
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
                    result = cjonstyle.get_tracking(context, product_url, headless=False)
                    print(f"⚠️ 미발송 주문일 것으로 예상했는데 조회됨: {result}")
                except TrackingNotAvailableYet as e:
                    print(f"✅ 예상대로 미발송으로 스킵됨: {e}")
        finally:
            browser_mod.save_state(context, cjonstyle.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
