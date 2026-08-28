"""GSSHOP 로그인용 크롬 프로필에 '구글 평판'만 심어보고, 점수가 오르는지 확인한다.

왜 필요한가:
    GSSHOP 로그인 폼의 reCAPTCHA Enterprise는 우리가 만드는 새 프로필에 항상
    `need`(체크박스 확인 요구)를 준다. 2026-08-29에 확인한 바로는 자동화가
    들켜서가 아니다 - 크롬을 직접 띄워 CDP로 붙으면 navigator.webdriver는 false고
    Runtime.enable 누출도 없는데도 `need`였다. 구글 검색/유튜브로 쿠키를 쌓는
    워밍업도 소용없었다. 남은 변수는 **오래 써온 브라우저에만 쌓이는 구글 평판**뿐이라,
    평소 쓰는 크롬의 구글 쿠키를 로그인용 프로필에 심어서 점수가 달라지는지 본다.

무엇을 하는가:
    1. 크롬이 완전히 종료되어 있는지 확인한다 (실행 중이면 쿠키 파일이 잠겨 있다).
    2. 평소 크롬 프로필에서 `Local State`와 `Default/Network/Cookies` 두 개만
       auth/chrome_profile_gsshop_seed 로 복사한다. (값 자체는 복호화하지 않는다 -
       같은 컴퓨터/계정이면 크롬이 스스로 읽는다.)
    3. 그 프로필로 크롬을 띄우자마자, **구글 계열(google/youtube/gstatic) 외의
       쿠키는 전부 지운다.** 쿠키 DB에는 다른 사이트 로그인도 통째로 들어 있기
       때문에, GSSHOP 로그인에 필요 없는 것은 남기지 않는다.
    4. 그 프로필로 GSSHOP 로그인을 시도하고 createAssessment 응답(pass/need)을 찍는다.
    5. 끝나면 쿠키 DB를 VACUUM해서 지운 쿠키의 흔적까지 없앤다.

실행 (크롬을 완전히 닫고):
    python scripts/seed_gsshop_login_profile.py

    --promote : 로그인까지 성공했을 때, 이 프로필을 어댑터가 쓰는
                auth/chrome_profile_gsshop 으로 승격한다(기존 프로필은 .bak로 남긴다).
    --keep    : 실패해도 만들어진 씨앗 프로필을 지우지 않는다(다시 시도해 보려는 경우).

실패하면(=여전히 need) 씨앗 프로필은 지운다. 평소 크롬 프로필은 읽기만 하고
절대 건드리지 않는다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
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

CHROME_USER_DATA = Path.home() / "AppData/Local/Google/Chrome/User Data"
SEED_KEY = "gsshop_seed"

# 남길 쿠키의 호스트 - reCAPTCHA 점수에 쓰이는 구글 계열만.
KEEP_HOST_MARKERS = ("google.com", "google.co.kr", "youtube.com", "gstatic.com", "googleapis.com")

# add_cookies가 받는 필드만 골라 다시 넣는다 (partitionKey 등은 버린다).
COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")

# 로그인 시도에 쓰는 주문 - test_gsshop_login.py와 같은 건이다.
TEST_PRODUCT_URL = "https://with.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3468580811&ecOrdTypCd=S"


def chrome_is_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return False
    return "chrome.exe" in out


def seed_profile(profile_dir: Path) -> None:
    """평소 크롬에서 쿠키 파일 두 개만 복사해 온다."""
    cookies_src = CHROME_USER_DATA / "Default" / "Network" / "Cookies"
    local_state_src = CHROME_USER_DATA / "Local State"
    if not cookies_src.exists():
        raise SystemExit(f"크롬 쿠키 파일을 찾지 못했습니다: {cookies_src}")

    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    (profile_dir / "Default" / "Network").mkdir(parents=True)
    shutil.copy2(cookies_src, profile_dir / "Default" / "Network" / "Cookies")
    shutil.copy2(local_state_src, profile_dir / "Local State")
    print(f"  평소 크롬에서 쿠키 파일을 복사했습니다 ({cookies_src.stat().st_size:,} 바이트).")


def keep_only_google(context) -> None:
    """구글 계열 외의 쿠키를 전부 지운다 (개인정보 최소화)."""
    all_cookies = context.cookies()
    keep = [c for c in all_cookies if any(m in c.get("domain", "") for m in KEEP_HOST_MARKERS)]
    context.clear_cookies()
    if keep:
        context.add_cookies([{k: c[k] for k in COOKIE_FIELDS if k in c} for c in keep])
    print(f"  쿠키 정리: 전체 {len(all_cookies)}개 중 구글 계열 {len(keep)}개만 남겼습니다.")
    names = sorted({c["name"] for c in keep if c.get("domain", "").endswith("google.com")})
    print(f"  google.com 쿠키: {names[:12]}{' ...' if len(names) > 12 else ''}")
    # 구글 로그인 쿠키가 있으면 평판이 훨씬 높다 - 있는지만 알려준다.
    signed_in = any(n in names for n in ("SID", "SAPISID", "__Secure-1PSID"))
    print(f"  구글 로그인 상태: {'있음 (점수에 유리)' if signed_in else '없음'}")


def try_login(context) -> tuple[bool, list[str]]:
    """씨앗 프로필로 GSSHOP 로그인을 시도한다. (성공여부, assess 응답들)"""
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

    page.goto(TEST_PRODUCT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if page.locator(gsshop.LOGIN_ID_SELECTOR).count() == 0:
        print("  로그인 페이지가 아닙니다 - 이 프로필에 GSSHOP 로그인이 이미 있습니다.")
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


def vacuum_cookies(profile_dir: Path) -> None:
    """지운 쿠키가 DB 빈 페이지에 남지 않도록 정리한다."""
    db = profile_dir / "Default" / "Network" / "Cookies"
    if not db.exists():
        return
    try:
        con = sqlite3.connect(db)
        con.execute("VACUUM")
        con.close()
        print("  쿠키 DB를 정리(VACUUM)했습니다.")
    except Exception as exc:
        print(f"  (쿠키 DB 정리 실패: {exc} - 씨앗 프로필을 지우면 함께 사라집니다)")


def promote(seed_dir: Path) -> None:
    real = browser_mod.real_chrome_profile_dir(gsshop.SITE_KEY)
    if real.exists():
        backup = real.with_name(real.name + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        real.rename(backup)
        print(f"  기존 프로필을 {backup.name} 으로 남겼습니다.")
    seed_dir.rename(real)
    print(f"  씨앗 프로필을 {real.name} 으로 승격했습니다 - 이제 어댑터가 이 프로필로 로그인합니다.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--promote", action="store_true", help="로그인 성공 시 어댑터용 프로필로 승격")
    parser.add_argument("--keep", action="store_true", help="실패해도 씨앗 프로필을 지우지 않음")
    args = parser.parse_args()

    load_dotenv()

    if chrome_is_running():
        raise SystemExit(
            "크롬이 실행 중입니다. 쿠키 파일이 잠겨 있어 복사할 수 없으니 "
            "크롬을 완전히 닫고 다시 실행해주세요."
        )

    seed_dir = browser_mod.real_chrome_profile_dir(SEED_KEY)
    print("1) 평소 크롬에서 쿠키 복사")
    seed_profile(seed_dir)

    ok = False
    assess: list[str] = []
    with sync_playwright() as p:
        browser_mod.remember_playwright(p)
        with browser_mod.real_chrome_cdp_context(SEED_KEY) as context:
            print("2) 구글 계열만 남기고 나머지 쿠키 삭제")
            keep_only_google(context)
            print("3) GSSHOP 로그인 시도")
            ok, assess = try_login(context)

    print()
    if ok:
        print(f"✅ 로그인 성공! (createAssessment 응답: {assess or '없음'})")
        print("   구글 평판을 빌려오면 GSSHOP도 사람 손 없이 로그인된다는 뜻입니다.")
        if args.promote:
            promote(seed_dir)
        else:
            print("   --promote 를 붙여 다시 실행하면 이 프로필을 어댑터용으로 승격합니다.")
            vacuum_cookies(seed_dir)
    else:
        print(f"❌ 여전히 막힙니다 (createAssessment 응답: {assess or '없음'}).")
        print("   구글 평판으로도 점수가 안 오릅니다 - 반자동(사람이 체크박스)이 최선입니다.")
        vacuum_cookies(seed_dir)
        if not args.keep:
            shutil.rmtree(seed_dir, ignore_errors=True)
            print("   씨앗 프로필을 지웠습니다.")


if __name__ == "__main__":
    main()
