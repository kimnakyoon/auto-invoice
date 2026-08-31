"""GSSHOP 로그인용 크롬 프로필에 구글 계정을 한 번만 로그인시킨다 (최초 1회).

왜 필요한가:
    GSSHOP 로그인 폼의 reCAPTCHA Enterprise는 우리가 만드는 프로필에 항상
    `need`(체크박스 확인 요구)를 주는데, **사람이 평소 쓰는 크롬으로 로그인하면
    체크박스가 아예 안 뜬다**(2026-08-29 사용자 확인). 그래서 못 뚫는 방어가
    아니라, 우리 프로필에 없는 신호가 있는 것이다.

    2026-08-29에 확인한 신호 차이는 **구글 로그인 여부**다. reCAPTCHA는 구글에
    로그인된 브라우저에 훨씬 높은 점수를 준다. 평소 크롬의 쿠키를 복사해
    빌려오는 방법은 크롬 127+의 앱 바운드 암호화 때문에 불가능하다(복사한
    프로필에서는 쿠키가 복호화되지 않는다 - 실측). 복사가 안 되면 **그 프로필에서
    직접 로그인하면 된다** - 이 스크립트가 그 한 번을 도와준다.

무엇을 하는가:
    1. 로그인용 프로필(auth/chrome_profile_gsshop)로 **평범한 크롬**을 띄운다.
       디버깅 포트도 자동화도 붙이지 않는다 - 구글은 자동화가 붙은 브라우저의
       로그인을 막기 때문에, 이 단계만큼은 완전히 평범한 창이어야 한다.
    2. 사람이 그 창에서 구글에 로그인하고 창을 닫는다. (평소 쓰는 계정이면 된다.
       GSSHOP 계정과는 아무 상관 없다.)
    3. 창이 닫히면, 그 프로필로 GSSHOP 로그인을 시도해서 점수가 달라졌는지
       (`pass`인지 `need`인지) 바로 확인해준다.

이 프로필은 지우지 않는다 - 쓸수록 이력이 쌓여 점수에 유리하고, 구글 로그인도
여기 남아 있어야 다음 실행에서 효과가 있다.

실행:
    python scripts/setup_gsshop_login_profile.py

    --skip-setup : 구글 로그인 창은 띄우지 않고 지금 프로필로 점수만 다시 확인한다.
    --relogin    : 프로필에 남은 GSSHOP 로그인을 지우고 다시 로그인시킨다
                   (구글 로그인은 그대로 둔다). 점수가 재현되는지 볼 때 쓴다.
    --times N    : 위 확인을 N번 반복한다.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice.suppliers import gsshop  # noqa: E402

# 로그인 시도에 쓰는 주문.
TEST_PRODUCT_URL = "https://with.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3468580811&ecOrdTypCd=S"

GOOGLE_LOGIN_URL = "https://accounts.google.com/"


def open_plain_chrome_for_google_login(profile_dir: Path) -> None:
    """자동화를 전혀 붙이지 않은 평범한 크롬 창을 띄우고, 닫힐 때까지 기다린다."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    print("크롬 창을 띄웁니다. 그 창에서 구글에 로그인한 뒤 창을 닫아주세요.")
    print("  - 평소 쓰시는 구글 계정이면 됩니다 (GSSHOP 계정과 무관합니다).")
    print("  - 로그인 뒤 유튜브나 구글 검색을 잠깐 써두면 더 좋습니다.")
    print("  - 창을 닫으면 이어서 GSSHOP 점수를 확인합니다.")
    proc = subprocess.Popen(
        [
            browser_mod.chrome_executable(),
            f"--user-data-dir={profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            GOOGLE_LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    print("창이 닫혔습니다.\n")


def forget_gsshop_login(context) -> None:
    """이 프로필의 GSSHOP 쿠키만 지운다 (구글 로그인은 건드리지 않는다)."""
    context.clear_cookies(domain=re.compile(r"gsshop\.com$"))


def google_signed_in(page) -> bool:
    page.goto("https://myaccount.google.com/", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    url = page.url
    return "myaccount.google.com" in url and "signin" not in url and "ServiceLogin" not in url


def try_gsshop_login(context) -> tuple[bool, list[str]]:
    """이 프로필로 GSSHOP 로그인을 시도한다. (성공여부, createAssessment 응답들)"""
    login_id = os.environ.get("GSSHOP_ID")
    login_pw = os.environ.get("GSSHOP_PW")
    if not login_id or not login_pw:
        raise SystemExit(".env에 GSSHOP_ID/GSSHOP_PW가 필요합니다.")

    assess: list[str] = []
    alerts: list[str] = []
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size(browser_mod.DESKTOP_VIEWPORT)
    page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))

    def on_response(response) -> None:
        if gsshop.RECAPTCHA_ASSESS_MARKER not in response.url:
            return
        try:
            assess.append(str((response.json() or {}).get("result")))
        except Exception:
            assess.append("(파싱실패)")

    page.on("response", on_response)

    print("구글 로그인 상태:", "있음 ✅" if google_signed_in(page) else "없음 ⚠️")

    page.goto(TEST_PRODUCT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if page.locator(gsshop.LOGIN_ID_SELECTOR).count() == 0:
        print("  이 프로필에 GSSHOP 로그인이 이미 남아 있습니다 (점수 확인은 못 했습니다).")
        return True, assess

    # fill()로 채우면 사이트가 빈 칸으로 인식한다 - 실제로 타이핑한다.
    page.locator(gsshop.LOGIN_ID_SELECTOR).click()
    page.locator(gsshop.LOGIN_ID_SELECTOR).press_sequentially(login_id, delay=90)
    page.locator(gsshop.LOGIN_PW_SELECTOR).click()
    page.locator(gsshop.LOGIN_PW_SELECTOR).press_sequentially(login_pw, delay=90)
    page.wait_for_timeout(600)
    page.locator(gsshop.LOGIN_BUTTON_SELECTOR).first.click()

    for _ in range(20):
        page.wait_for_timeout(1500)
        if "login.gs" not in page.url:
            return True, assess
        if alerts:
            print(f"  사이트가 로그인을 거부했습니다: {alerts[0].strip()}")
            return False, assess
        if assess and assess[-1] == gsshop.RECAPTCHA_BLOCKED_RESULT:
            return False, assess
    return False, assess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-setup", action="store_true", help="구글 로그인 창 없이 점수만 확인")
    parser.add_argument("--relogin", action="store_true", help="GSSHOP 쿠키를 지우고 다시 로그인시킨다")
    parser.add_argument("--times", type=int, default=1, help="확인 반복 횟수")
    args = parser.parse_args()

    load_dotenv()
    profile_dir = browser_mod.real_chrome_profile_dir(gsshop.SITE_KEY)

    if not args.skip_setup:
        open_plain_chrome_for_google_login(profile_dir)

    results = []
    for n in range(1, max(1, args.times) + 1):
        if args.times > 1:
            print(f"\n[{n}/{args.times}]", end=" ")
        print("이 프로필로 GSSHOP 로그인을 시도합니다...")
        with sync_playwright() as p:
            browser_mod.remember_playwright(p)
            with browser_mod.real_chrome_cdp_context(gsshop.SITE_KEY) as context:
                if args.relogin:
                    forget_gsshop_login(context)
                ok, assess = try_gsshop_login(context)
        results.append((ok, assess))
        print(f"  -> {'성공' if ok else '실패'} (assess={assess or '없음'})")

    print()
    if all(ok for ok, _ in results):
        print(f"✅ 사람 손 없이 로그인됐습니다! ({len(results)}번 시도 전부 성공)")
        print("   구글 로그인이 점수를 올려준 것이므로, 어댑터를 완전 자동으로 바꿀 수 있습니다.")
    else:
        failed = [a for ok, a in results if not ok]
        print(f"❌ {len(failed)}/{len(results)}번 막혔습니다 (막힌 응답: {failed}).")
        print("   재현되지 않으면 어댑터를 바꾸면 안 됩니다 - 반자동 경로를 남겨둬야 합니다.")


if __name__ == "__main__":
    main()
