"""샵마인 '발송대상' 엑셀에서 CJ온스타일 주문만 뽑아서 보여주는 확인용 스크립트.

CJ온스타일도 이제 다른 공급사와 똑같이 조회 배치에 포함되므로 평소에는
이 스크립트를 쓸 일이 없다. 어떤 주문이 CJ온스타일 건으로 분류되는지
확인하거나(조회가 이상할 때), 그 목록만 따로 보고 싶을 때 쓴다.

실행:
    python scripts/extract_cjonstyle_orders.py "발송대상 엑셀 경로"
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice.shopmine import excel_io  # noqa: E402

CJONSTYLE_DOMAIN = "cjonstyle.com"


def main() -> None:
    if len(sys.argv) != 2:
        print('사용법: python scripts/extract_cjonstyle_orders.py "발송대상 엑셀 경로"')
        sys.exit(1)

    orders = excel_io.read_pending_orders(sys.argv[1])
    cj_orders = [o for o in orders if CJONSTYLE_DOMAIN in o.product_url]

    if not cj_orders:
        print("CJ온스타일 미발송 주문이 없습니다.")
        return

    print(f"CJ온스타일 미발송 주문 {len(cj_orders)}건:\n")
    for o in cj_orders:
        print(f"- 마켓 주문번호: {o.order_id}")
        print(f"  URL: {o.product_url}")
        if o.recipient_name:
            print(f"  수령인: {o.recipient_name}")
        if o.order_option:
            print(f"  주문옵션: {o.order_option}")
        print()

    print("이 주문들은 GUI 실행 시 실제 크롬 브라우저를 통해 자동으로 조회됩니다.")


if __name__ == "__main__":
    main()
