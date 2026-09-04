"""송장 자동화 전 과정을 한 번에 실행한다 (터미널용).

실제 로직은 auto_invoice/pipeline.py 에 있고, 바탕화면 아이콘(gui.pyw)도
같은 파이프라인을 쓴다. 이 스크립트는 옵션을 받아 넘기고 결과를 출력한다.

사용 예:
    python scripts/run_all.py                             # 기본 상한 100건
    python scripts/run_all.py --limit 5 --max-apply 5      # 소량 테스트
    python scripts/run_all.py --stop-before-apply          # 일괄등록까지만
    python scripts/run_all.py --resume                    # 멈춘 지점부터 이어서
    python scripts/run_all.py --csv "~/Desktop/송장업로드_....csv"  # 6~8단계만
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
from auto_invoice.report import LOG_DIR  # noqa: E402


def log(msg=""):
    print(msg, flush=True)


class _Tee:
    """콘솔에 찍히는 것을 파일에도 그대로 남긴다.

    조회 결과는 logs/run_*.json 에 남지만, 공급사 어댑터가 도중에 찍는 말
    ("[lotteon] 주문목록 API를 읽지 못해 화면 목록으로 대신합니다" 같은 것)은
    콘솔에만 나오고 사라졌다. 그래서 나중에 '왜 그 건은 예정 문구가 비었나'를
    되짚을 수 없었다 (2026-09-04). 창을 닫아도 남도록 같은 내용을 파일로도 쓴다.
    """

    def __init__(self, stream, path: Path):
        self._stream = stream
        self._file = open(path, "a", encoding="utf-8")

    def write(self, text):
        self._stream.write(text)
        try:
            self._file.write(text)
            self._file.flush()
        except Exception:  # noqa: BLE001 - 로그 파일 때문에 실행이 깨지면 안 된다
            pass

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _tee_console(started: datetime) -> Path | None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"console_{started:%Y%m%d_%H%M%S}.log"
        sys.stdout = _Tee(sys.stdout, path)
        sys.stderr = _Tee(sys.stderr, path)
        return path
    except Exception:  # noqa: BLE001 - 로그 파일을 못 만들어도 실행은 계속한다
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="샵마인 송장 자동화 전 과정 실행")
    p.add_argument("--limit", type=int, default=None,
                   help="공급사 조회를 몇 건까지 할지 (테스트 시 작게)")
    p.add_argument("--max-apply", type=int, default=100,
                   help="이 건수를 넘으면 반영 단계로 넘어가지 않고 멈춘다 (기본 100)")
    p.add_argument("--tab", default="배송중",
                   help="작업할 샵마인 탭 이름 (다른 탭이면 이 탭으로 옮긴다)")
    p.add_argument("--stop-before-apply", action="store_true",
                   help="일괄등록까지만 하고 [송장번호수정]은 사람이 직접 누른다")
    p.add_argument("--headless", action="store_true",
                   help="공급사 조회를 브라우저 창 없이 (최초 로그인 이후에만)")
    p.add_argument("--resume", action="store_true",
                   help="지난 실행이 중간에 멈춘 지점부터 이어서 실행한다 "
                        "(이미 내보낸 주문목록을 그대로 쓰고, 이미 조회한 주문은 건너뛴다)")
    p.add_argument("--csv", default=None,
                   help="이미 만들어둔 업로드용 CSV로 6~8단계만 실행 "
                        "(상한 초과로 멈춘 뒤 확인하고 재실행할 때)")
    args = p.parse_args()
    console_log = _tee_console(datetime.now())

    log("=" * 60)
    if args.csv:
        log(f"기존 CSV로 6~8단계 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    elif args.resume:
        log(f"멈춘 지점부터 이어서 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    else:
        log(f"송장 자동화 전체 실행  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    log("=" * 60)

    if args.csv:
        result = pipeline.run_from_csv(
            args.csv, max_apply=args.max_apply, tab=args.tab,
            stop_before_apply=args.stop_before_apply, log=log)
    else:
        result = pipeline.run_full(
            limit=args.limit, max_apply=args.max_apply, tab=args.tab,
            stop_before_apply=args.stop_before_apply, headless=args.headless,
            resume=args.resume, log=log)

    log("")
    log("=" * 60)
    log(pipeline.summarize(result))
    if console_log:
        log(f"실행 로그: {console_log}")
    log("=" * 60)

    if result.stopped_reason and not result.applied:
        return 2 if "상한" in result.stopped_reason else 1
    return 1 if result.applied_bad else 0


if __name__ == "__main__":
    sys.exit(main())
