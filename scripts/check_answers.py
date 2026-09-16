"""남긴 1:1 문의에 공급사 답변이 달렸는지 확인해 장부에 적고, 바탕화면에 '문의내역' 시트만 든 문의 결과 엑셀을 만든다.

    python scripts/check_answers.py                 # 최근 문의 전부 확인 + 바탕화면 문의결과_*.xlsx('문의내역' 시트)
    python scripts/check_answers.py --no-excel      # 장부만 갱신
    python scripts/check_answers.py --site lotteon  # 한 사이트만
    python scripts/check_answers.py --force         # 이미 답변을 받은 문의도 다시 확인

[문의] 버튼(inquiry.run)이 문의를 남긴 뒤 같은 확인을 스스로 하므로(오늘 남긴 것은
제외), 이 스크립트는 문의를 남기지 않고 답변만 바로 보고 싶을 때 쓴다. 송장조회는
답변을 보지 않고(사용자 요청 2026-09-11), 송장조회 결과 엑셀도 건드리지 않는다(사용자
요청 2026-09-16 - 답변은 문의 결과 엑셀의 '문의내역' 시트로).
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
    parser.add_argument("--no-excel", action="store_true", help="엑셀은 만들지 않고 장부만 갱신")
    parser.add_argument("--site", action="append", default=None, help="이 사이트만 (여러 번 줄 수 있음)")
    parser.add_argument("--force", action="store_true", help="이미 답변을 받은 문의도 다시 확인")
    parser.add_argument("--headless", action="store_true", help="브라우저 창 없이 (로그인 세션이 있을 때만)")
    args = parser.parse_args()

    before = inquiry_answers.answered_ids(inquiry_answers.ledger_by_id())
    by_id = inquiry_answers.refresh(None, headless=args.headless, force=args.force,
                                    sites=set(args.site) if args.site else None, log=print)
    answered = [e for e in by_id.values() if (e.get("answer_check") or {}).get("answer")]
    new_ids = inquiry_answers.answered_ids(by_id) - before
    print()
    print(f"장부 {len(by_id)}건 중 답변 받은 문의 {len(answered)}건 (이번에 새로 읽은 답변 {len(new_ids)}건)")
    for e in answered:
        check = e["answer_check"]
        first = next((ln for ln in inquiry_answers.condense_answer(check["answer"], e.get("message") or "").splitlines()
                      if ln.strip()), "")
        mark = "새 " if e["order_id"] in new_ids else ""
        print(f"  {mark}{e['site']} {e['order_id']} {e.get('recipient_name') or ''}: "
              f"{check.get('answered_on') or ''} {first[:90]}")

    if args.no_excel:
        return
    run = inquiry.InquiryRun(answer_entries=inquiry_answers.recent_entries(by_id), new_answer_ids=set(new_ids))
    if not run.answer_entries:
        print(f"최근 {inquiry_answers.ANSWER_LOOKBACK_DAYS}일에 남긴 문의가 없어 엑셀은 만들지 않았습니다.")
        return
    inquiry.save_result_excel(run, log=print)


if __name__ == "__main__":
    main()
