"""지금 이 컴퓨터의 크롬에 로그인되어 있는 세션(쿠키)을 그대로 가져와서,
자동화 프로그램이 사용하는 auth/*_state.json 을 만든다.

이렇게 하면 프로그램에서 최초 로그인을 headed 브라우저로 직접 할 필요 없이,
지금 크롬에 로그인된 세션을 그대로 이어받아 바로 자동 로그인 상태로 시작한다.

동작 방식:
1. 실제 크롬 프로필(Default)의 쿠키 관련 파일만 임시 폴더로 복사한다.
   (쿠키 값을 직접 복호화하지 않는다 - 최신 크롬은 쿠키를 "앱 바운드 암호화"로
   보호해서 크롬 프로세스 밖에서 직접 복호화하는 게 사실상 불가능하다. 대신
   설치되어 있는 진짜 크롬으로 그 프로필을 열어서, 크롬 스스로 복호화하게 한다.)
2. Playwright로 그 임시 프로필을 이용해 실제 크롬(channel="chrome")을 한 번 띄운다.
3. 각 공급사 사이트로 이동해 로그인 세션이 있는지 확인하고, 해당 사이트 쿠키만
   골라 auth/{site}_state.json 으로 저장한다.
4. 임시로 복사한 프로필은 다른 모든 사이트의 로그인 세션이 통째로 들어있으므로
   개인정보 보호를 위해 작업 즉시 삭제한다.

주의:
- 실행 전 크롬을 완전히 종료해야 프로필 파일을 복사할 수 있다 (실행 중이면
  쿠키 파일이 잠겨 있어 복사에 실패할 수 있다).
- 네이버는 계정 전환 기능 특성상 크롬에는 한 번에 한 계정만 로그인되어
  있으므로, 지금 크롬에 로그인된 계정이 어느 쪽인지 실행 중에 물어본다.
  다만 2026-08-28 실측 결과 **네이버는 이 방식이 통하지 않는다** - 크롬에서
  가져온 쿠키로는 Playwright 브라우저에서 로그인 상태가 유지되지 않고
  로그인 페이지로 튕긴다(무신사와 같은 양상으로, 세션을 브라우저에
  묶어두는 것으로 보인다). 네이버는 헤드리스가 아닌 실행으로 직접 1회
  로그인하는 방법밖에 없다. 같은 날 확인한 바로는 패션플러스/GSSHOP은
  이 방식으로 정상 동작했다.
- 가져온 세션이 실제로 유효한지는 저장 시점에 알 수 없으므로, 덮어쓰기
  전에 기존 파일을 <사이트>_state.json.bak 으로 남긴다. 가져온 뒤에는 각
  사이트 테스트 스크립트로 확인하고, 문제가 있으면 .bak을 되돌리면 된다.
- 무신사는 이 스크립트로 세션을 가져올 수 없다 - Cloudflare로 보이는 봇
  차단이 있어서, 쿠키만 다른 브라우저로 옮기면 서버가 세션을 무효 처리한다
  (로그인 페이지로 리다이렉트됨). 무신사는 대신 scripts/musinsa_login_setup.py로
  최초 1회 직접 로그인해야 한다 (자세한 이유는 그 스크립트와
  suppliers/musinsa.py의 설명 참고).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = REPO_ROOT / "auth"
CHROME_USER_DATA = Path.home() / "AppData/Local/Google/Chrome/User Data"
CHROME_EXE = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
TMP_PROFILE = AUTH_DIR / "_chrome_import_tmp"

SITES = {
    "lotteon": {
        "domains": ["lotteon.com", "lpoint.com"],
        "check_url": "https://www.lotteon.com/",
    },
    "gmarket": {
        "domains": ["gmarket.co.kr", "esmplus.com"],
        "check_url": "https://www.esmplus.com/",
    },
    "ssg": {
        "domains": ["ssg.com"],
        "check_url": "https://member.ssg.com/",
    },
    "fashionplus": {
        "domains": ["fashionplus.co.kr"],
        "check_url": "https://www.fashionplus.co.kr/mypage",
    },
    "gsshop": {
        "domains": ["gsshop.com"],
        "check_url": "https://with.gsshop.com/",
    },
    "cjonstyle": {
        "domains": ["cjonstyle.com"],
        "check_url": "https://base.cjonstyle.com/p/myzone/orderList?listType=ORDER&orderStatus=ALL",
    },
}
NAVER_DOMAINS = ["naver.com"]
NAVER_CHECK_URL = "https://new.pay.naver.com/home/my"


def copy_profile() -> None:
    if TMP_PROFILE.exists():
        shutil.rmtree(TMP_PROFILE)
    TMP_PROFILE.mkdir(parents=True)
    shutil.copy2(CHROME_USER_DATA / "Local State", TMP_PROFILE / "Local State")

    default_src = CHROME_USER_DATA / "Default"
    default_dst = TMP_PROFILE / "Default"
    default_dst.mkdir()

    for name in ("Cookies", "Cookies-journal", "Preferences"):
        src = default_src / name
        if src.exists():
            shutil.copy2(src, default_dst / name)

    network_src = default_src / "Network"
    if network_src.exists():
        network_dst = default_dst / "Network"
        network_dst.mkdir()
        for name in ("Cookies", "Cookies-journal"):
            src = network_src / name
            if src.exists():
                shutil.copy2(src, network_dst / name)


def filter_state(state: dict, domains: list[str]) -> dict:
    cookies = [c for c in state.get("cookies", []) if any(d in c["domain"] for d in domains)]
    origins = [o for o in state.get("origins", []) if any(d in o["origin"] for d in domains)]
    return {"cookies": cookies, "origins": origins}


def write_state(site_key: str, state: dict) -> None:
    """쿠키를 하나도 못 찾았으면 저장하지 않는다.

    예전에는 무조건 덮어썼는데, 지금 크롬에 그 사이트 로그인이 안 되어 있으면
    빈 파일을 써서 **이미 잘 동작하던 세션을 날려버렸다**. 크롬에 없는 사이트는
    건드리지 않고 그대로 두는 게 맞다.
    """
    out = AUTH_DIR / f"{site_key}_state.json"
    if not state["cookies"]:
        existing = " (기존 세션 파일은 그대로 둡니다)" if out.exists() else ""
        print(f"  -> {site_key}: 크롬에 로그인 세션이 없어 건너뜁니다{existing}.")
        return
    # 가져온 쿠키가 그 사이트에서 실제로 유효한지는 여기서 알 수 없다(네이버가
    # 그랬듯 다른 브라우저로 옮기면 무효 처리하는 사이트가 있다). 잘 되던
    # 세션을 되돌릴 수 있도록 덮어쓰기 전에 .bak으로 남긴다.
    if out.exists():
        shutil.copy2(out, out.with_suffix(".json.bak"))
    out.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> {site_key}: 쿠키 {len(state['cookies'])}개 저장 ({out})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--naver-account",
        choices=["1", "2", "skip"],
        help="지금 크롬에 로그인된 네이버 계정 (1=NAVER_ID, 2=NAVER_ID2, skip=건너뛰기). "
        "생략하면 실행 중에 물어봅니다. 콘솔 입력을 받을 수 없는 환경에서 쓰세요.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("크롬 프로필 복사 중...")
    try:
        copy_profile()
    except PermissionError:
        print(
            "크롬이 실행 중이라 쿠키 파일을 복사할 수 없습니다.\n"
            "크롬을 완전히 종료(작업 표시줄/트레이 포함)한 뒤 다시 실행하세요."
        )
        sys.exit(1)

    if not CHROME_EXE.exists():
        print(f"크롬 실행파일을 찾을 수 없습니다: {CHROME_EXE}")
        sys.exit(1)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(TMP_PROFILE),
            executable_path=str(CHROME_EXE),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("\n공급사 사이트 로그인 세션 확인 중...")
        for site_key, cfg in SITES.items():
            page.goto(cfg["check_url"], wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            full_state = context.storage_state()
            filtered = filter_state(full_state, cfg["domains"])
            write_state(site_key, filtered)

        print("\n네이버 로그인 세션 확인 중...")
        page.goto(NAVER_CHECK_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        full_state = context.storage_state()
        naver_filtered = filter_state(full_state, NAVER_DOMAINS)

        if naver_filtered["cookies"]:
            print(f"  네이버 로그인 세션 {len(naver_filtered['cookies'])}개 쿠키 발견.")
            if args.naver_account:
                answer = args.naver_account.replace("skip", "s")
                print(f"  --naver-account={args.naver_account} 로 지정됨.")
            else:
                answer = input(
                    "  지금 크롬에 로그인된 네이버 계정은 어느 쪽인가요? "
                    "[1] NAVER_ID  [2] NAVER_ID2  [s] 건너뛰기: "
                ).strip().lower()
            if answer == "1":
                write_state("naver", naver_filtered)
            elif answer == "2":
                write_state("naver2", naver_filtered)
            else:
                print("  건너뜀.")
        else:
            print("  네이버 로그인 세션을 찾지 못했습니다 (건너뜀).")

        context.close()

    shutil.rmtree(TMP_PROFILE, ignore_errors=True)
    print("\n임시 프로필 삭제 완료. 이제 프로그램이 방금 저장된 세션으로 자동 로그인됩니다.")


if __name__ == "__main__":
    main()
