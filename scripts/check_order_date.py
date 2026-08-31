"""주문상세 화면에서 '주문일'이 어디에 있는지 사람 눈으로 확인하는 도구.

왜 필요한가: 결과 엑셀의 '주문일' 칸이 공급사에 따라 비어 있다. 화면에서
'주문일자' 같은 라벨 뒤 40자 안의 날짜만 주문일로 인정하는데(order_date.py),
어떤 사이트는 그 규칙에 안 걸린다. 어느 사이트가 왜 안 걸리는지는 실제 화면을
봐야 알 수 있어서, 주문상세 화면을 띄워두고 그 화면의 텍스트를 같이 떠준다.

실행:
    python scripts/check_order_date.py nsmall            # 기본 주문 URL로
    python scripts/check_order_date.py nsmall <주문URL>  # URL을 직접 줄 때

창은 WAIT_SECONDS(기본 600초) 동안 열려 있고, 사람이 창을 닫으면 바로 끝난다.
화면 텍스트/스크린샷은 dumps/ 아래에 남긴다.
"""

import os
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from auto_invoice import browser as browser_mod  # noqa: E402
from auto_invoice import order_date as order_date_mod  # noqa: E402
from auto_invoice.suppliers import base as supplier_base  # noqa: E402
from auto_invoice.suppliers import registry  # noqa: E402

# 각 사이트의 실제 주문상세 URL.
DEFAULT_URLS = {
    "nsmall": "https://m.nsmall.com/cs/order-detail?orderNum=560824001998",
    "thehyundai": "https://hi.thehyundai.com/mypage/order/detail?ordNo=260808001647316&isLogin=Y",
    "hmall": "https://www.hmall.com/mo/mpa/selectOrdPTCPup?ordNo=20260818019104&selectTypeGbcd=",
    "lotteimall": "https://www.lotteimall.com/mypage/getOrderDtlInfo.lotte?ord_no=20260824K87597",
    "lotteon": "https://www.lotteon.com/p/order/claim/orderDetail?odNo=2026082316683630",
    "ssg": "https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo=2026082458D816",
    "elevenst": "https://buy.11st.co.kr/order/BuyManager.tmall?method=getOrderDetailInfo&ordNo=20260825095273858&isSSL=Y",
    "cjonstyle": "https://base.cjonstyle.com/p/myzone/orderInfo/20260827009262",
    "fashionplus": "https://www.fashionplus.co.kr/mypage/order/detail/141262620",
    "gmarket": "https://my.gmarket.co.kr/ko/pc/detail/basic/5522257883",
    "naver": "https://orders.pay.naver.com/order/status/2026082523369571?returnUrl=https%3A%2F%2Fpay.naver.com%2Fpc%2Fhistory",
    "musinsa": "https://www.musinsa.com/order/order-detail/202608250706390001",
    "akplaza": "https://www.digital-akplaza.com/mypage/orderList/202608250102769",
    "gsshop": "https://with.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo=3468580811&ecOrdTypCd=S",
}

WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "600"))
DUMP_DIR = Path(__file__).resolve().parent.parent / "dumps"

# 화면에서 '날짜처럼 보이는 것'을 전부 찾아 앞뒤 맥락과 함께 보여준다.
_DATE_LIKE = re.compile(
    r"20\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}"
    r"|(?<!\d)\d{1,2}\s*[월./-]\s*\d{1,2}\s*일"
    r"|(?<!\d)20\d{6}(?!\d)"
)


def _adapter_of(site_key: str):
    for module in set(registry._ADAPTERS.values()):
        if getattr(module, "SITE_KEY", None) == site_key:
            return module
    raise SystemExit(f"'{site_key}' 어댑터를 찾지 못했습니다. "
                     f"가능한 값: {', '.join(sorted(DEFAULT_URLS))}")


def _report(text: str, where: str) -> None:
    """지금 파서가 이 텍스트에서 무엇을 보는지 그대로 보여준다."""
    print(f"\n[{where}] 글자 수 {len(text)}")
    found = order_date_mod.from_text(text)
    print(f"  현재 파서 판정: {found or '못 읽음'}")

    hits = [label for label in order_date_mod._LABELS if label in text]
    print(f"  라벨 발견: {', '.join(hits) if hits else '없음'}")
    for label in hits:
        for m in re.finditer(re.escape(label), text):
            after = text[m.end():m.end() + order_date_mod._LABEL_WINDOW]
            print(f"    '{label}' 뒤 40자: {after!r}")

    print("  날짜처럼 보이는 것 (앞뒤 30자):")
    seen = set()
    for m in _DATE_LIKE.finditer(text):
        if m.group() in seen:
            continue
        seen.add(m.group())
        print(f"    {m.group()!r}  <-  {text[max(0, m.start() - 30):m.end() + 30]!r}")


def _inspect(page, site_key: str) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    print(f"\n=== 주문상세 화면 도착: {page.url}")

    texts: list[tuple[str, str]] = []
    for i, frame in enumerate(page.frames):
        try:
            texts.append((f"frame{i} {frame.url[:80]}", frame.inner_text("body")))
        except Exception as e:  # noqa: BLE001 - 못 읽는 프레임은 건너뛴다
            print(f"  frame{i} 읽기 실패: {e}")

    for where, text in texts:
        _report(text, where)

    dump = DUMP_DIR / f"{site_key}_text.txt"
    dump.write_text("\n\n".join(f"##### {w}\n{t}" for w, t in texts), encoding="utf-8")
    shot = DUMP_DIR / f"{site_key}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception as e:  # noqa: BLE001
        print(f"  스크린샷 실패: {e}")
    print(f"\n텍스트: {dump}\n스크린샷: {shot}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"사용법: python scripts/check_order_date.py <사이트> [주문URL]\n"
                         f"사이트: {', '.join(sorted(DEFAULT_URLS))}")
    site_key = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_URLS.get(site_key)
    if not url:
        raise SystemExit(f"'{site_key}'의 기본 주문 URL이 없습니다. URL을 직접 넣어주세요.")

    adapter = _adapter_of(site_key)
    # 어댑터는 with_order_date -> _page_text 로 주문상세 텍스트를 한 번 읽는다.
    # 그 자리가 '주문상세 화면에 도착한 순간'이라 여기에 끼어든다.
    real_page_text = supplier_base._page_text

    def patched(page):
        """어댑터가 주문상세 화면을 읽는 그 순간에 끼어든다."""
        _inspect(page, site_key)
        print(f"\n창을 열어둡니다 (최대 {WAIT_SECONDS}초). 다 보셨으면 창을 닫으세요.")
        for _ in range(WAIT_SECONDS):
            if page.is_closed():
                break
            page.wait_for_timeout(1000)
        return real_page_text(page)

    supplier_base._page_text = patched

    with sync_playwright() as p:
        browser, context = browser_mod.get_context(p, site_key, headless=False)
        try:
            print(f"[{site_key}] {url} 여는 중...")
            try:
                result = adapter.get_tracking(context, url, headless=False)
                print(f"조회 결과: 송장 {result.tracking_no} / {result.courier} / "
                      f"주문일 {result.order_date}")
            except Exception as e:  # noqa: BLE001 - 조회 결과는 여기서 중요하지 않다
                print(f"조회는 {type(e).__name__}로 끝남: {e}")
        finally:
            browser_mod.save_state(context, site_key)
            browser.close()


if __name__ == "__main__":
    main()
