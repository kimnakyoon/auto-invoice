"""1단계 검증: 지마켓 어댑터가 실제로 송장번호를 가져오는지 단독으로 확인한다.

실행:
    python scripts/test_gmarket_adapter.py

최초 실행 시 브라우저 창이 뜨면 직접 지마켓에 로그인하세요(아이디는 .env의
GMARKET_ID로 자동 입력됨). 로그인이 완료되면 자동으로 감지해서 이어서
진행합니다. Cloudflare 봇 확인 화면이 뜨면 체크박스를 직접 눌러 통과해야
합니다. 로그인 세션은 auth/gmarket_state.json 에 저장되어 다음 실행부터는
다시 로그인하지 않아도 됩니다.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import gmarket  # noqa: E402

# 대화에서 확인했던 실제 주문 URL/송장번호로 결과가 맞는지 검증한다.
TEST_PRODUCT_URL = "https://my.gmarket.co.kr/ko/pc/detail/basic/5522257883"
EXPECTED_TRACKING_NO = "501707425705"
EXPECTED_COURIER = "CJ대한통운"


def main() -> None:
    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, gmarket.SITE_KEY, headless=False)
        try:
            result = gmarket.get_tracking(context, TEST_PRODUCT_URL, headless=False)
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
            browser_mod.save_state(context, gmarket.SITE_KEY)
            browser.close()


if __name__ == "__main__":
    main()
