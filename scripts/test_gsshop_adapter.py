"""1단계 검증: GSSHOP 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_gsshop_adapter.py

최초 실행 시 브라우저 창이 뜨면 직접 GSSHOP에 로그인하세요. 로그인이 완료되면
자동으로 감지해서 이어서 진행합니다. 로그인 세션은 auth/gsshop_state.json 에
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
from auto_invoice.suppliers import gsshop  # noqa: E402
from auto_invoice.suppliers.base import TrackingNotAvailableYet  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
# 상품URL 호스트는 with.gsshop.com / www.gsshop.com 두 가지로 들어오므로
# 양쪽을 모두 넣어둔다.
# 기대값이 (None, None)이면 "아직 송장번호 미발급이 정상"이라는 뜻이다 - 확인
# 시점에 배송준비중이던 주문이라, 나중에 실제로 발송되면 송장번호가 찍힌다.
# 3468580811(with): 상품 1개, 롯데택배(내부코드 HD) / 311920754250
# 3469348858(www) : 확인 시점 배송준비중 - 호스트가 www여도 로그인/파싱이
#                   되는지(entry-data를 읽어내는지) 보는 용도
TEST_CASES = [
    (
        "https://with.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3468580811&ecOrdTypCd=S",
        "311920754250",
        "롯데택배",
    ),
    (
        "https://www.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3469348858&ecOrdTypCd=S",
        None,
        None,
    ),
]


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, gsshop.SITE_KEY, headless=False)
        try:
            for product_url, expected_tracking_no, expected_courier in TEST_CASES:
                print(f"--- {product_url}")
                try:
                    result = gsshop.get_tracking(context, product_url, headless=False)
                except TrackingNotAvailableYet as e:
                    # 로그인/파싱까지는 정상이고 아직 발송 전이라는 뜻이다.
                    print("아직 송장번호 미발급:", e)
                    if expected_tracking_no is None:
                        print("✅ 미발급이 예상된 주문입니다 (호스트 처리는 정상).")
                    else:
                        print(f"⚠️ 예상값(송장:{expected_tracking_no}, 택배사:{expected_courier})이 나와야 합니다.")
                    continue
                print("송장번호:", result.tracking_no)
                print("택배사:", result.courier)
                if expected_tracking_no is None:
                    print("ℹ️ 확인 시점에는 미발급이던 주문인데 이제 발송되었습니다 (정상).")
                elif result.tracking_no == expected_tracking_no and result.courier == expected_courier:
                    print("✅ 예상했던 값과 일치합니다.")
                else:
                    print(f"⚠️ 예상값(송장:{expected_tracking_no}, 택배사:{expected_courier})과 다릅니다.")
        finally:
            browser_mod.save_state(context, gsshop.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
