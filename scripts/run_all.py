"""송장 자동화 전 과정을 한 번에 실행한다 (터미널용).

실제 로직은 auto_invoice/pipeline.py 에 있고, 바탕화면 아이콘(gui.pyw)도
같은 파이프라인을 쓴다. 이 스크립트는 옵션을 받아 넘기고 결과를 출력한다.

사용 예:
    python scripts/run_all.py                             # 기본 상한 100건
    python scripts/run_all.py --limit 5 --max-apply 5      # 소량 테스트
    python scripts/run_all.py --stop-before-apply          # 일괄등록까지만
    python scripts/run_all.py --csv "~/Desktop/송장업로드_....csv"  # 4~6단계만
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice import pipeline  # noqa: E402


def log(msg=""):
    print(msg, flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="샵마인 송장 자동화 전 과정 실행")
    p.add_argument("--limit", type=int, default=None,
                   help="공급사 조회를 몇 건까지 할지 (테스트 시 작게)")
    p.add_argument("--max-apply", type=int, default=100,
                   help="이 건수를 넘으면 반영 단계로 넘어가지 않고 멈춘다 (기본 100)")
    p.add_argument("--tab", default="배송중", help="엑셀을 내보낼 탭 이름")
    p.add_argument("--stop-before-apply", action="store_true",
                   help="일괄등록까지만 하고 [송장번호수정]은 사람이 직접 누른다")
    p.add_argument("--headless", action="store_true",
                   help="공급사 조회를 브라우저 창 없이 (최초 로그인 이후에만)")
    p.add_argument("--csv", default=None,
                   help="이미 만들어둔 업로드용 CSV로 4~6단계만 실행 "
                        "(상한 초과로 멈춘 뒤 확인하고 재실행할 때)")
    p.add_argument("--skip-cjonstyle", action="store_true",
                   help="CJ온스타일(실제 크롬으로 조회하는 느린 경로)을 건너뛴다")
    args = p.parse_args()

    log("=" * 60)
    if args.csv:
        log(f"기존 CSV로 4~6단계 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    else:
        log(f"송장 자동화 전체 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    log("=" * 60)

    if args.csv:
        result = pipeline.run_from_csv(
            args.csv, max_apply=args.max_apply,
            stop_before_apply=args.stop_before_apply, log=log)
    else:
        result = pipeline.run_full(
            limit=args.limit, max_apply=args.max_apply, tab=args.tab,
            stop_before_apply=args.stop_before_apply, headless=args.headless,
            skip_cjonstyle=args.skip_cjonstyle, log=log)

    log("")
    log("=" * 60)
    log(pipeline.summarize(result))
    log("=" * 60)

    if result.stopped_reason and not result.applied:
        return 2 if "상한" in result.stopped_reason else 1
    return 1 if result.applied_bad else 0


if __name__ == "__main__":
    sys.exit(main())
