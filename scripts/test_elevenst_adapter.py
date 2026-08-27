"""1단계 검증: 11번가 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_elevenst_adapter.py

ELEVENST_ID/ELEVENST_PW로 완전 자동 로그인하므로 사람이 개입할 필요가 없다.
로그인 세션은 auth/elevenst_state.json 에 저장되어 다음 실행부터는 쿠키로
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
from auto_invoice.suppliers import elevenst  # noqa: E402
from auto_invoice.suppliers.base import TrackingNotAvailableYet  # noqa: E402

# 샵마인 내보내기 파일에 실제로 들어있던 주문 URL과, 11번가 배송추적 화면에서
# 눈으로 확인한 송장번호/택배사로 결과가 맞는지 검증한다.
TEST_CASES = [
    (
        "https://buy.11st.co.kr/order/BuyManager.tmall?method=getOrderDetailInfo&ordNo=20260823094887892&isSSL=Y",
        "304318936344",
        "CJ대한통운",
    ),
    (
        "https://buy.11st.co.kr/order/BuyManager.tmall?method=getOrderDetailInfo&ordNo=20260825095273858&isSSL=Y",
        "318150159084",
        "롯데택배",
    ),
]


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, elevenst.SITE_KEY, headless=False)
        try:
            for product_url, expected_tracking_no, expected_courier in TEST_CASES:
                print(f"--- {product_url}")
                try:
                    result = elevenst.get_tracking(context, product_url, headless=False)
                except TrackingNotAvailableYet as e:
                    print(f"⚠️ 미발송으로 스킵됨: {e}")
                    continue
                print("송장번호:", result.tracking_no)
                print("택배사:", result.courier)
                ok = result.tracking_no == expected_tracking_no and result.courier == expected_courier
                if ok:
                    print("✅ 예상했던 값과 일치합니다.")
                else:
                    print(f"⚠️ 예상값(송장:{expected_tracking_no}, 택배사:{expected_courier})과 다릅니다.")
        finally:
            browser_mod.save_state(context, elevenst.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
