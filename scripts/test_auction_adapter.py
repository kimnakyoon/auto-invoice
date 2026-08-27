"""1단계 검증: 옥션 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_auction_adapter.py
    python scripts/test_auction_adapter.py --input "C:\\...\\주문목록-선택-....xls"

AUCTION_ID/AUCTION_PW로 완전 자동 로그인하므로 사람이 개입할 필요가 없다.
로그인 세션은 auth/auction_state.json 에 저장되어 다음 실행부터는 쿠키로
바로 조회된다.

검증은 두 갈래다:

  1. (인자 없이) 주문번호를 직접 지정해 배송조회가 맞는지 본다. 로그인/쿠키,
     송장번호 추출, 택배사 정규화("롯데" -> "롯데택배")까지 확인된다.
  2. (--input 을 주면) 샵마인 내보내기 파일에서 옥션 주문을 읽어 "주문옵션 +
     수령인"으로 주문을 찾아내는 경로까지 확인한다. 이 경로는 수령인 이름(고객
     개인정보)이 있어야 검증할 수 있어서, 이름을 스크립트에 적어두지 않고 실행할
     때 실제 파일에서 읽는다 (README의 "고객 개인정보는 기록하지 않는다" 원칙).
"""

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.shopmine import excel_io  # noqa: E402
from auto_invoice.suppliers import auction  # noqa: E402
from auto_invoice.suppliers.base import AdapterError, TrackingNotAvailableYet  # noqa: E402
from auto_invoice.suppliers.registry import get_adapter  # noqa: E402

DETAIL_URL = "https://escrow.auction.co.kr/Close/OrderProcessDetailLayer.aspx?order_no={}"

# (옥션 주문번호, 기대 송장번호, 기대 택배사) - 옥션 화면에서 눈으로 확인한 값.
# 개인정보가 아닌 주문번호/송장번호만 적는다.
DIRECT_CASES = [
    ("2569178034", "501707423071", "CJ대한통운"),
    ("2569085269", "301774818754", "CJ대한통운"),
    ("2568375348", "512000857025", "CJ대한통운"),
    # 택배사 정규화가 "롯데" -> "롯데택배"로 되는지 같이 본다 (사용자 요청 5번)
    ("2568284256", "318459754981", "롯데택배"),
]


def check_direct(context) -> None:
    """상품URL에 주문번호가 들어있을 때의 경로 (목록을 훑지 않는다)."""
    print("=== 1) 주문번호 직접 지정 ===")
    for order_no, expected_tracking_no, expected_courier in DIRECT_CASES:
        print(f"--- 주문번호 {order_no}")
        try:
            result = auction.get_tracking(context, DETAIL_URL.format(order_no), headless=False)
        except TrackingNotAvailableYet as e:
            print(f"⚠️ 미발송으로 스킵됨: {e}")
            continue
        print("송장번호:", result.tracking_no, "/ 택배사:", result.courier)
        if result.tracking_no == expected_tracking_no and result.courier == expected_courier:
            print("✅ 예상했던 값과 일치합니다.")
        else:
            print(f"⚠️ 예상값(송장:{expected_tracking_no}, 택배사:{expected_courier})과 다릅니다.")


def check_lookup(context, input_path: str) -> None:
    """샵마인 내보내기의 "주문옵션 + 수령인"으로 주문을 찾아내는 경로."""
    print("\n=== 2) 주문옵션 + 수령인으로 찾기 ===")
    if not Path(input_path).exists():
        print(f"❌ 파일을 찾을 수 없습니다: {input_path}")
        return
    orders = [o for o in excel_io.read_pending_orders(input_path) if get_adapter(o.product_url) is auction]
    if not orders:
        print(f"{input_path} 에 옥션 주문이 없습니다 - 건너뜁니다.")
        return

    for order in orders:
        print(f"--- 마켓 주문번호 {order.order_id} / 주문옵션={order.order_option!r}")
        try:
            result = auction.get_tracking(
                context,
                order.product_url,
                headless=False,
                order_option=order.order_option,
                recipient_name=order.recipient_name,
            )
        except TrackingNotAvailableYet as e:
            print(f"⚠️ 미발송으로 스킵됨: {e}")
            continue
        except AdapterError as e:
            print(f"❌ 실패: {e}")
            continue
        print(f"✅ 송장번호: {result.tracking_no} / 택배사: {result.courier}")


def main() -> None:
    parser = argparse.ArgumentParser(description="옥션 어댑터 단독 검증")
    parser.add_argument(
        "--input",
        default=None,
        help="샵마인 발송대상 엑셀 경로 (주면 '주문옵션+수령인으로 찾기' 경로까지 검증한다)",
    )
    args = parser.parse_args()

    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, auction.SITE_KEY, headless=False)
        try:
            check_direct(context)
            if args.input:
                check_lookup(context, args.input)
            else:
                print("\n(--input 으로 샵마인 내보내기 파일을 주면 '주문옵션+수령인으로 찾기'도 검증합니다.)")
        finally:
            browser_mod.save_state(context, auction.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
