"""주문일이 2일 지난 주문에 공급사 사이트로 1:1 문의를 남기고, 전에 남긴 문의의 답변을 가져온다 (GUI [문의] 버튼과 같은 일).

    python scripts/inquire.py --dry-run          # 무엇을 남길지만 보기 (답변 확인도 안 함)
    python scripts/inquire.py --limit 1          # 한 건만 실제로 남겨보기
    python scripts/inquire.py --no-answers       # 남기기만, 답변 확인은 건너뜀
    python scripts/inquire.py --excel 바탕화면\송장조회결과_20260904_092714.xlsx

답변 확인은 읽은 송장조회결과 엑셀의 '사유' 칸을 제자리에서 고친다 - 엑셀은 닫아둘 것.
"""

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice import inquiry  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--excel", default=None,
                        help="송장조회결과 엑셀 경로 (기본: 바탕화면에서 가장 최근 파일)")
    parser.add_argument("--limit", type=int, default=None, help="이번에 남길 최대 건수")
    parser.add_argument("--dry-run", action="store_true", help="남기지 않고 대상만 보여준다")
    parser.add_argument("--headless", action="store_true", help="브라우저 창 없이 (로그인 세션이 있을 때만)")
    parser.add_argument("--no-answers", action="store_true", help="전에 남긴 문의의 답변 확인은 건너뛴다")
    args = parser.parse_args()

    result = inquiry.run(args.excel, limit=args.limit, headless=args.headless,
                         dry_run=args.dry_run, check_answers=not args.no_answers, log=print)
    print()
    print(inquiry.summarize(result))
    if not args.dry_run:
        inquiry.save_result_excel(result, log=print)
        saved = inquiry.save_run_log(result)
        if saved:
            print(f"상세 로그: {saved}")


if __name__ == "__main__":
    main()
