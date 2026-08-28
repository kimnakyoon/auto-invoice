"""GSSHOP 자동 로그인만 따로 검증한다.

auth/gsshop_state.json(저장된 로그인 세션)을 **쓰지 않고** 빈 브라우저로
시작하기 때문에, 반드시 로그인 경로를 타게 된다. .env의 GSSHOP_ID/GSSHOP_PW로
자동 로그인이 실제로 되는지 확인하는 용도다.

로그인하는 동안에만 크롬 창이 떴다가 닫힌다 - 로그인 폼의 reCAPTCHA가
"크롬을 직접 실행해 CDP로 붙었고, 그 프로필이 구글에 로그인되어 있는" 창에만
통과 점수를 주기 때문이다(자세한 내용은 suppliers/gsshop.py). 사람이 타이핑하거나
체크박스를 누를 일은 없다.

**단, 그 프로필의 구글 로그인은 최초 1회 사람이 해줘야 한다** -
scripts/setup_gsshop_login_profile.py를 실행하면 된다. 이 스크립트에서
'로봇이 아닙니다' 체크박스가 떴다면 그 구글 로그인이 풀린 것이다.

실행:
    python scripts/test_gsshop_login.py

성공하면 새로 로그인한 세션을 auth/gsshop_state.json에 저장한다. 실패하면
기존 세션 파일은 그대로 둔다(멀쩡한 세션을 망가뜨리지 않기 위해).
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

# test_gsshop_adapter.py와 같은 주문 - 로그인이 끝난 뒤 실제 조회까지 되는지 본다.
TEST_PRODUCT_URL = "https://with.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3468580811&ecOrdTypCd=S"
EXPECTED_TRACKING_NO = "311920754250"
EXPECTED_COURIER = "롯데택배"

# 조회 쪽은 평소 실행과 같은 조건(번들 Chromium, headless)으로 둔다 - 로그인
# 창에서 받은 쿠키가 여기로 제대로 옮겨오는지까지 봐야 하기 때문이다. 로그인
# 창은 어댑터가 따로 띄운다.
QUERY_HEADLESS = True
# get_tracking의 headless 인자는 "사람이 안 보고 있다"는 뜻이다. True로 준다 -
# 이제 이 사이트는 사람 손 없이 로그인되는 것이 정상이라, 체크박스가 뜨면
# 기다리지 말고 실패해야 검증이 된다(기다리면 사람이 눌러서 통과시켜 버린다).
# 체크박스가 떴다면 로그인 프로필의 구글 로그인이 풀린 것이다 -
# scripts/setup_gsshop_login_profile.py를 다시 실행하면 된다.
NOBODY_WATCHING = True


def main() -> None:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("GSSHOP_PW"):
        print("⚠️ .env에 GSSHOP_PW가 비어 있습니다. 자동 로그인을 건너뜁니다.")
        return

    print("ℹ️ 로그인하는 동안만 크롬 창이 떴다가 닫힙니다 (사람이 할 일은 없습니다).")

    with sync_playwright() as p:
        # 로그인용 진짜 크롬 창은 이 인스턴스로 만들어진다.
        browser_mod.remember_playwright(p)
        browser = p.chromium.launch(headless=QUERY_HEADLESS)
        # 저장된 세션을 일부러 쓰지 않는다 - 로그인 경로를 반드시 타게 하려고.
        context = browser.new_context()
        try:
            result = gsshop.get_tracking(context, TEST_PRODUCT_URL, headless=NOBODY_WATCHING)
            print("✅ 로그인 + 조회 성공")
            print("   송장번호:", result.tracking_no)
            print("   택배사:", result.courier)
            if result.tracking_no == EXPECTED_TRACKING_NO and result.courier == EXPECTED_COURIER:
                print("   예상했던 값과 일치합니다.")
            else:
                print(f"   ⚠️ 예상값(송장:{EXPECTED_TRACKING_NO}, 택배사:{EXPECTED_COURIER})과 다릅니다.")
            browser_mod.save_state(context, gsshop.SITE_KEY)
            print("   새 로그인 세션을 auth/gsshop_state.json에 저장했습니다.")
        except Exception as e:
            print(f"❌ 실패: {type(e).__name__}: {e}")
            print("   (기존 세션 파일은 건드리지 않았습니다)")
            shot = Path(__file__).resolve().parent.parent / "logs" / "gsshop_login_실패화면.png"
            shot.parent.mkdir(exist_ok=True)
            try:
                pages = [pg for pg in context.pages if not pg.is_closed()]
                if pages:
                    pages[-1].screenshot(path=str(shot), full_page=True)
                    print(f"   마지막 화면을 저장했습니다: {shot}")
            except Exception as shot_err:
                print(f"   (화면 저장 실패: {shot_err})")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
