import argparse
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice import result_excel  # noqa: E402
from auto_invoice.orchestrator import run  # noqa: E402


def default_output_path() -> str:
    filename = f"송장자동화_{date.today():%Y%m%d}.csv"
    return str(Path.home() / "Desktop" / filename)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="샵마인 '발송대상' 엑셀을 읽어 공급사 송장번호를 조회하고, "
        "'발송정보일괄등록(수정용)' 형식의 엑셀을 생성합니다."
    )
    parser.add_argument("--input", required=True, help="샵마인 [주문관리 > 발송대상 > 엑셀파일생성]으로 받은 엑셀 경로")
    parser.add_argument(
        "--output",
        default=None,
        help="생성할 업로드용 CSV 경로 (기본: 바탕화면\\송장자동화_YYYYMMDD.csv)",
    )
    parser.add_argument("--limit", type=int, default=None, help="한 번에 처리할 주문 수 제한")
    parser.add_argument(
        "--headless", action="store_true", help="브라우저 창 없이 실행합니다. 최초 로그인 이후에만 사용하세요."
    )
    args = parser.parse_args()
    output_path = args.output or default_output_path()

    report = run(args.input, output_path, limit=args.limit, headless=args.headless)

    counts = report.summary()
    print(f"완료: 성공 {counts['success']} / 실패 {counts['fail']} / 스킵 {counts['skip']}")
    failure_lines = report.failure_lines()
    if failure_lines:
        print("\n실패한 주문 (샵마인에서 직접 확인해주세요):")
        for line in failure_lines:
            print(line)
    # 조회 자체를 못 한 주문(아직 지원하지 않는 사이트, 취소/품절)은 실패와
    # 성격이 달라 따로 묶어 보여준다 - 사람이 직접 처리해야 하는 목록이다.
    for title, lines in report.attention_blocks():
        print(f"\n{title}:")
        for line in lines:
            print(line)
    report_path = report.save()
    print(f"상세 리포트: {report_path}")
    # 조회 결과는 JSON 말고 사람이 바로 열어볼 엑셀로도 남긴다 (바탕화면).
    result_excel.save_run_result(
        report.entries, counts,
        applied_label="미반영 (업로드 전)",
        paths=(("입력 엑셀", args.input),
               ("업로드용 파일", output_path if counts["success"] else None),
               ("상세 로그", report_path)))
    if counts["success"] > 0:
        print(f"업로드용 파일: {output_path}")
        print("이 파일을 검토한 뒤 샵마인 [발송정보일괄등록(수정용)]으로 직접 업로드해주세요.")


if __name__ == "__main__":
    main()
