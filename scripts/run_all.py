"""송장 자동화 전 과정을 한 번에 실행한다.

    1. 샵마인 [배송중] 탭에서 주문 목록 엑셀 내보내기   (shopmine/export.py)
    2. 공급사에서 송장번호 조회 -> 업로드용 CSV 생성    (orchestrator.py)
    3. CSV를 [발송정보일괄등록(수정용)]으로 업로드      (shopmine/upload.py)
    4. [일괄등록] - 송장번호(수정용) 컬럼에 반영
    5. [송장번호수정] - 쇼핑몰까지 실제 반영

기본값은 --dry-run 이 아니라 '5단계 직전에 멈추는' 것이 아니라, 건수 상한과
화면 검증을 통과하는 한 끝까지 간다. 다만 다음 세 지점에서 언제든 멈춘다:

  - 화면이 예상과 다르면 (창이 안 뜸, 버튼이 가려짐, 커서가 엉뚱한 곳)
  - 조회 성공 건수가 --max-apply 를 넘으면
  - 마지막 확인 대화상자가 말하는 건수가 CSV 건수와 다르면

사용 예:
    python scripts/run_all.py --limit 5 --max-apply 5
    python scripts/run_all.py --stop-before-apply     # 4단계까지만
"""

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice.orchestrator import run as run_orchestrator  # noqa: E402
from auto_invoice.shopmine import export, upload  # noqa: E402

WORK_DIR = Path(__file__).resolve().parent.parent / "work"


def log(msg=""):
    print(msg, flush=True)


def step(n, title):
    log()
    log(f"[{n}/5] {title}")


def read_csv_order_ids(path):
    """업로드용 CSV에서 '고객주문번호' 목록을 읽는다."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip()]
    if not rows:
        return []
    header, *data = rows
    try:
        idx = header.index("고객주문번호")
    except ValueError:
        idx = 0
    return [r[idx].strip() for r in data if r[idx].strip()]


def apply_csv(csv_path, order_ids, args):
    """3~5단계 실행 (엑셀 내보내기/조회는 이미 끝난 상태에서)."""
    rows = len(order_ids)

    step(3, "샵마인 송장수정모드 켜고 업로드 창 열기")
    try:
        upload.ensure_edit_mode(log=log)

        step(4, f"CSV 일괄등록 ({rows}건)")
        upload.bulk_register(csv_path, log=log)
    except upload.UploadError as e:
        log(f"\n중단: {e}")
        return 1

    if args.stop_before_apply:
        log()
        log("4단계까지 완료했습니다. 화면에서 '송장번호(수정용)' 컬럼을 확인한 뒤")
        log("[송장번호수정] 버튼을 직접 눌러주세요.")
        return 0

    step(5, f"[송장번호수정]으로 쇼핑몰까지 반영 ({rows}건, 한 건씩)")
    results = upload.apply_one_by_one(order_ids, log=log)
    try:
        upload.filter_grid("", log=log)      # 목록 원상복구
    except upload.UploadError as e:
        log(f"  경고: 목록 필터를 되돌리지 못했습니다 - {e}")

    ok = [o for o, s in results if s.startswith("오류없음")]
    bad = [(o, s) for o, s in results if not s.startswith("오류없음")]

    log()
    log("=" * 60)
    log(f"완료: 반영 성공 {len(ok)}건 / 실패 {len(bad)}건 (조회 성공 {rows}건 기준)")
    for o, s in bad:
        log(f"  실패 {o}: {s}")
    if bad:
        log("실패 건은 샵마인에서 직접 확인해주세요.")
    log("=" * 60)
    return 0 if not bad else 1


def run_from_csv(csv_path, args):
    """--csv 로 기존 CSV를 이어받아 3~5단계만 실행한다."""
    log("=" * 60)
    log(f"기존 CSV로 3~5단계 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    log("=" * 60)
    if not csv_path.exists():
        log(f"중단: CSV가 없습니다 - {csv_path}")
        return 1
    order_ids = read_csv_order_ids(csv_path)
    log(f"  CSV: {csv_path} ({len(order_ids)}건)")
    if not order_ids:
        log("중단: CSV에 주문번호가 없습니다.")
        return 1
    if len(order_ids) > args.max_apply:
        log(f"중단: {len(order_ids)}건이 상한({args.max_apply}건)을 넘습니다.")
        return 2
    return apply_csv(csv_path, order_ids, args)


def main():
    p = argparse.ArgumentParser(description="샵마인 송장 자동화 전 과정 실행")
    p.add_argument("--limit", type=int, default=None,
                   help="공급사 조회를 몇 건까지 할지 (테스트 시 작게)")
    p.add_argument("--max-apply", type=int, default=30,
                   help="이 건수를 넘으면 5단계로 넘어가지 않고 멈춘다 (기본 30)")
    p.add_argument("--tab", default="배송중", help="엑셀을 내보낼 탭 이름")
    p.add_argument("--stop-before-apply", action="store_true",
                   help="4단계까지만 하고 [송장번호수정]은 사람이 직접 누른다")
    p.add_argument("--headless", action="store_true",
                   help="공급사 조회를 브라우저 창 없이 (최초 로그인 이후에만)")
    p.add_argument("--csv", default=None,
                   help="이미 만들어둔 업로드용 CSV로 3~5단계만 실행 "
                        "(상한 초과로 멈춘 뒤 확인하고 재실행할 때)")
    args = p.parse_args()

    if args.csv:
        return run_from_csv(Path(args.csv), args)

    WORK_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = WORK_DIR / f"주문목록_{stamp}.xls"
    csv_path = WORK_DIR / f"송장업로드_{stamp}.csv"

    log("=" * 60)
    log(f"송장 자동화 전체 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    log("=" * 60)

    # --- 1단계 --------------------------------------------------------
    step(1, f"샵마인 [{args.tab}] 탭에서 주문 목록 내보내기")
    try:
        export.export_to(export_path, tab_title=args.tab, log=log)
    except export.ExportError as e:
        log(f"\n중단: {e}")
        return 1

    # --- 2단계 --------------------------------------------------------
    step(2, "공급사에서 송장번호 조회")
    report = run_orchestrator(str(export_path), str(csv_path),
                              limit=args.limit, headless=args.headless)
    counts = report.summary()
    log(f"  성공 {counts['success']} / 실패 {counts['fail']} / 스킵 {counts['skip']}")
    for line in report.failure_lines():
        log(f"  {line}")
    log(f"  상세 리포트: {report.save()}")

    if counts["success"] == 0:
        log("\n조회 성공 건이 없어 여기서 종료합니다 (샵마인은 건드리지 않음).")
        return 0

    order_ids = read_csv_order_ids(csv_path)
    rows = len(order_ids)
    log(f"  업로드용 CSV: {csv_path} ({rows}건)")

    if rows > args.max_apply:
        log(f"\n중단: 반영 대상이 {rows}건으로 상한({args.max_apply}건)을 넘습니다.")
        log("CSV는 만들어 두었으니 확인 후 --max-apply 를 올려 다시 실행하세요.")
        return 2

    # --- 3~5단계 ------------------------------------------------------
    return apply_csv(csv_path, order_ids, args)


if __name__ == "__main__":
    sys.exit(main())
