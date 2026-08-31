"""공급사 어댑터 하나를 상품URL로 직접 돌려본다 (사이트별 test_* 스크립트 대체).

어느 어댑터를 쓸지는 실제 실행과 똑같이 상품URL의 도메인으로 정한다
(suppliers/registry.py). 그래서 사이트마다 스크립트를 따로 둘 필요가 없다.

    # 지금 살아 있는 주문 하나로 조회가 되는지
    python scripts/check_adapter.py "https://www.lotteon.com/p/order/claim/orderDetail?odNo=..."

    # 여러 건 한 번에 (계정이 여러 개인 무신사/네이버는 계정별 주문을 하나씩)
    python scripts/check_adapter.py "URL1" "URL2"

    # 저장된 로그인 세션을 무시하고 로그인 경로부터 확인
    python scripts/check_adapter.py --fresh "URL"

    # 옵션이 여러 개인 주문에서 맞는 송장을 고르는지 / 옥션처럼 수령인이 필요한 곳
    python scripts/check_adapter.py --option "블랙 / 260" --recipient 홍길동 "URL"

주문 URL은 오래되면 사이트에서 사라진다. 그래서 예상 송장번호를 파일에
적어두지 않고 --expect 로 그때그때 넘긴다 - 적어둔 주문이 만료되면 실패가
어댑터 문제인지 주문 만료인지 구분할 수 없다.

--fresh 는 성공했을 때만 새 세션을 저장한다. 실패하면 멀쩡한 기존 세션을
건드리지 않고, 마지막 화면을 logs/ 에 남긴다.
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
from auto_invoice.report import LOG_DIR  # noqa: E402
from auto_invoice.suppliers.registry import get_adapter  # noqa: E402


def _save_screenshot(context, site_key: str) -> None:
    """어디서 어긋났는지 사람이 볼 수 있게 마지막 화면을 남긴다."""
    shot = LOG_DIR / f"{site_key}_실패화면.png"
    try:
        pages = [pg for pg in context.pages if not pg.is_closed()]
        if not pages:
            return
        LOG_DIR.mkdir(exist_ok=True)
        pages[-1].screenshot(path=str(shot), full_page=True)
        print(f"   마지막 화면: {shot}")
    except Exception as e:  # noqa: BLE001 - 화면 저장 실패가 결과를 덮으면 안 된다
        print(f"   (화면 저장 실패: {e})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("urls", nargs="+", help="공급사 주문상세 상품URL (샵마인 엑셀의 '상품URL')")
    parser.add_argument("--expect", default=None, help="예상 송장번호 (넘기면 결과와 비교한다)")
    parser.add_argument("--option", default=None, help="주문옵션 - 한 주문에 상품이 여러 개일 때")
    parser.add_argument("--recipient", default=None, help="수령인 이름 - 옥션처럼 URL만으로 주문을 못 고르는 곳")
    parser.add_argument("--fresh", action="store_true",
                        help="저장된 로그인 세션을 쓰지 않는다 (로그인 경로 검증)")
    parser.add_argument("--show", action="store_true", help="브라우저 창을 띄운다")
    args = parser.parse_args()

    # 한 번에 한 어댑터만 본다 - 로그인 세션도 브라우저 컨텍스트도 사이트별이라,
    # 섞어 넣으면 엉뚱한 어댑터로 조회하게 된다.
    adapters = {}
    for url in args.urls:
        found = get_adapter(url)
        if found is None:
            print(f"❌ 이 주소에 맞는 어댑터가 없습니다: {url}")
            sys.exit(1)
        adapters.setdefault(found.SITE_KEY, found)
    if len(adapters) > 1:
        print(f"❌ 사이트가 섞여 있습니다({', '.join(adapters)}). 한 번에 한 사이트씩 넣어주세요.")
        sys.exit(1)
    adapter = next(iter(adapters.values()))
    headless = not args.show
    print(f"어댑터: {adapter.SITE_KEY} / 창 {'띄움' if args.show else '없음'}"
          f"{' / 저장된 세션 무시' if args.fresh else ''}")

    failed = False
    with sync_playwright() as p:
        if args.fresh:
            # 세션 없이 시작해 반드시 로그인 경로를 타게 한다. 자동 로그인이
            # 따로 크롬을 띄우는 어댑터(현대몰/CJ온스타일 등)를 위해 Playwright
            # 인스턴스를 기억시켜둔다 - get_context()를 안 거치기 때문이다.
            browser_mod.remember_playwright(p)
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            browser_mod.block_heavy_resources(context)
        else:
            browser, context = browser_mod.get_context(p, adapter.SITE_KEY, headless=headless)

        try:
            for url in args.urls:
                print(f"\n--- {url}")
                extra = {}
                if getattr(adapter, "WANTS_RECIPIENT_NAME", False) and args.recipient:
                    extra["recipient_name"] = args.recipient
                try:
                    result = adapter.get_tracking(context, url, headless=headless,
                                                  order_option=args.option, **extra)
                except Exception as e:  # noqa: BLE001 - 어떤 예외든 사람이 보고 판단한다
                    failed = True
                    print(f"❌ {type(e).__name__}: {e}")
                    _save_screenshot(context, adapter.SITE_KEY)
                    continue

                print(f"   송장번호: {result.tracking_no}")
                print(f"   택배사:   {result.courier}")
                if result.order_date:
                    print(f"   주문일:   {result.order_date}")
                if result.delivery_note:
                    print(f"   예정문구: {result.delivery_note}")
                if args.expect and result.tracking_no != args.expect:
                    failed = True
                    print(f"⚠️ 예상한 송장번호({args.expect})와 다릅니다.")
                else:
                    print("✅ 조회 성공")

            if args.fresh and not failed:
                browser_mod.save_state(context, adapter.SITE_KEY)
                print(f"\n새 로그인 세션을 저장했습니다: {browser_mod.state_path(adapter.SITE_KEY)}")
            elif args.fresh:
                print("\n실패가 있어 세션을 저장하지 않았습니다 (기존 세션은 그대로입니다).")
        finally:
            browser.close()

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
