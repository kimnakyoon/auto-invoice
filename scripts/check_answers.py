"""남긴 1:1 문의에 공급사 답변이 달렸는지 확인해 장부에 적고, 최신 송장조회 결과 엑셀의 '사유' 칸에 붙인다.

    python scripts/check_answers.py                 # 최근 문의 전부 확인 + 바탕화면 최신 결과 엑셀 갱신
    python scripts/check_answers.py --no-excel      # 장부만 갱신
    python scripts/check_answers.py --site lotteon  # 한 사이트만
    python scripts/check_answers.py --force         # 이미 답변을 받은 문의도 다시 확인
    python scripts/check_answers.py --excel 바탕화면\송장조회결과_20260911_101500.xlsx

송장조회(run_all.py, GUI [전부 자동])는 조회 직후 '주문일지연' 건에 대해 같은 확인을
스스로 하므로, 이 스크립트는 그 사이에 온 답변을 바로 보고 싶을 때 쓴다.
"""

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice import inquiry, inquiry_answers  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--excel", default=None,
                        help="사유 칸을 고칠 송장조회결과 엑셀 (기본: 바탕화면에서 가장 최근 파일)")
    parser.add_argument("--no-excel", action="store_true", help="엑셀은 건드리지 않고 장부만 갱신")
    parser.add_argument("--site", action="append", default=None, help="이 사이트만 (여러 번 줄 수 있음)")
    parser.add_argument("--force", action="store_true", help="이미 답변을 받은 문의도 다시 확인")
    parser.add_argument("--headless", action="store_true", help="브라우저 창 없이 (로그인 세션이 있을 때만)")
    args = parser.parse_args()

    by_id = inquiry_answers.refresh(None, headless=args.headless, force=args.force,
                                    sites=set(args.site) if args.site else None, log=print)
    answered = [e for e in by_id.values() if (e.get("answer_check") or {}).get("answer")]
    print()
    print(f"장부 {len(by_id)}건 중 답변 받은 문의 {len(answered)}건")
    for e in answered:
        check = e["answer_check"]
        first = next((ln for ln in inquiry_answers.condense_answer(check["answer"], e.get("message") or "").splitlines()
                      if ln.strip()), "")
        print(f"  {e['site']} {e['order_id']} {e.get('recipient_name') or ''}: "
              f"{check.get('answered_on') or ''} {first[:90]}")

    if args.no_excel:
        return
    path = Path(args.excel) if args.excel else inquiry.find_latest_result_excel()
    if path is None or not path.exists():
        print("송장조회결과 엑셀이 없어 사유 칸은 고치지 않았습니다.")
        return
    try:
        changed = inquiry_answers.update_excel(path, by_id)
    except PermissionError:
        print(f"엑셀이 열려 있어 고치지 못했습니다 - 닫고 다시 실행해주세요: {path}")
        return
    print(f"{path.name}: 사유 칸 {changed}개 갱신")


if __name__ == "__main__":
    main()
