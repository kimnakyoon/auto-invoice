"""1단계 검증: 네이버페이 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_naver_adapter.py

최초 1회는 계정별로 사람이 직접 로그인해야 한다 (네이버가 자동 비밀번호
입력을 감지해 보안 확인을 요구하기 때문 - 롯데온/지마켓과 동일한 이유).
헤드리스로 실행하면 로그인 필요 시 바로 실패하니, 처음에는
`headless=False`로 한 번 돌려서 아이디는 자동 입력된 상태에서 비밀번호와
보안 확인만 직접 완료해주면 된다. 로그인 세션은 auth/naver_state.json
(첫 번째 계정), auth/naver2_state.json(두 번째 계정)에 저장되어 다음
실행부터는 재로그인하지 않는다.
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

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
# 첫 번째 계정(NAVER_ID) 소유의 주문 - 배송중, 롯데택배로 발송됨.
ACCOUNT1_ORDER_URL = "https://orders.pay.naver.com/order/status/2026082227166731?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"
EXPECTED_TRACKING_NO = "410798786683"
EXPECTED_COURIER = "롯데택배"

# 두 번째 계정(NAVER_ID2) 소유의 주문(취소완료 - 송장번호는 없지만, 계정
# 전환 로직이 실제로 두 번째 계정까지 찾아가는지 확인하는 용도).
ACCOUNT2_ORDER_URL = "https://orders.pay.naver.com/order/status/2026082523369571?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory"


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, naver.SITE_KEY, headless=True)
        try:
            result = naver.get_tracking(context, ACCOUNT1_ORDER_URL, headless=True)
            print("[계정1] 송장번호:", result.tracking_no)
            print("[계정1] 택배사:", result.courier)
            if result.tracking_no == EXPECTED_TRACKING_NO and result.courier == EXPECTED_COURIER:
                print("✅ 예상했던 송장번호/택배사와 일치합니다.")
            else:
                print(
                    f"⚠️ 예상값(송장번호={EXPECTED_TRACKING_NO}, 택배사={EXPECTED_COURIER})과 다릅니다. "
                    "주문 상태가 바뀌었을 수 있습니다."
                )

            print()
            print("[계정전환] 두 번째 계정 소유 주문 조회 시도 (계정1에는 없어야 정상)...")
            try:
                result2 = naver.get_tracking(context, ACCOUNT2_ORDER_URL, headless=True)
                print("[계정2] 송장번호:", result2.tracking_no, "/ 택배사:", result2.courier)
                print("✅ 계정 전환 로직이 두 번째 계정에서 주문을 찾았습니다.")
            except TrackingNotAvailableYet as e:
                print("✅ 계정 전환 로직이 두 번째 계정에서 주문을 찾았습니다 (아직 송장 미발급):", e)
            except OrderCancelled as e:
                # 이 검증용 주문은 취소완료 상태다. raise_if_cancelled가 생기기
                # 전에는 TrackingNotAvailableYet으로 뭉뚱그려졌지만, 지금은
                # 취소/품절로 따로 분류되므로 이것도 정상 통과로 본다.
                print("✅ 계정 전환 로직이 두 번째 계정에서 주문을 찾았습니다 (취소된 주문):", e)
        finally:
            browser_mod.save_state(context, naver.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
