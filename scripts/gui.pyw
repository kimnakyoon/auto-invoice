"""송장 자동화 - 더블클릭으로 실행하는 간단한 창.

바탕화면의 최근 엑셀 파일을 자동으로 찾아 보여주고, [실행] 버튼 하나로
샵마인 '발송정보일괄등록(수정용)' 업로드용 파일을 생성한다.

실행 중에 마우스를 움직이거나 다른 창을 앞으로 가져오면 샵마인 조작이
중단된다. 그때를 위해 [멈춘 지점부터 다시 시작] 버튼을 둔다 - 이미 끝낸
송장조회는 다시 하지 않고 멈춘 자리부터 이어서 한다 (checkpoint.py).
"""

import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox

if sys.platform == "win32" and sys.stdout is not None:
    # pythonw.exe(콘솔 없는 실행)에서는 stdout/stderr가 None이라 건드리면 안 된다.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_invoice import checkpoint, inquiry, pipeline, result_excel  # noqa: E402
from auto_invoice.orchestrator import run as run_orchestrator  # noqa: E402

DESKTOP = Path.home() / "Desktop"

WINDOW_WIDTH = 600
WINDOW_HEIGHT = 520
RIGHT_MARGIN = 40  # 화면 오른쪽 가장자리와의 여백(px)


def _place_right_center(root: tk.Tk) -> None:
    """창을 화면의 오른쪽 중앙에 위치시킨다."""
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = screen_width - WINDOW_WIDTH - RIGHT_MARGIN
    y = (screen_height - WINDOW_HEIGHT) // 2
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")


def find_latest_export() -> Path | None:
    candidates = sorted(DESKTOP.glob("*.xls"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def default_output_path() -> Path:
    return DESKTOP / f"송장자동화_{date.today():%Y%m%d}.csv"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("송장 자동화")
        _place_right_center(root)
        root.minsize(520, 460)

        self.selected_file: Path | None = find_latest_export()
        self._output_path: Path | None = None
        self._result_excel_path: Path | None = None
        self._queue: "queue.Queue" = queue.Queue()

        # --- 전자동: 샵마인에서 받아서 반영까지 ---
        tk.Label(
            root,
            text="샵마인을 켜둔 뒤 아래 버튼을 누르세요. ([배송중] 탭은 알아서 엽니다)",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 4))

        # [송장] 옆에 [문의] - 송장조회 결과 엑셀의 '2일 지남' 주문에
        # 공급사 사이트로 "○○○ 배송 언제 시작하나요?"를 남긴다 (inquiry.py).
        top_row = tk.Frame(root)
        top_row.pack(pady=(0, 2))
        self.auto_button = tk.Button(
            top_row,
            text="⚡  송장",
            font=("맑은 고딕", 14, "bold"),
            bg="#188038",
            fg="white",
            activebackground="#0d652d",
            activeforeground="white",
            command=self.start_full_auto,
        )
        self.auto_button.pack(side="left")
        self.inquiry_button = tk.Button(
            top_row,
            text="✉  문의",
            font=("맑은 고딕", 12, "bold"),
            bg="#1a73e8",
            fg="white",
            activebackground="#174ea6",
            activeforeground="white",
            command=self.start_inquiry,
        )
        self.inquiry_button.pack(side="left", padx=(10, 0), fill="y")

        limit_frame = tk.Frame(root)
        limit_frame.pack(pady=(0, 6))
        tk.Label(limit_frame, text="최대 반영 건수", fg="#5f6368").pack(side="left")
        self.max_apply = tk.Spinbox(limit_frame, from_=1, to=200, width=5)
        self.max_apply.delete(0, "end")
        self.max_apply.insert(0, "100")
        self.max_apply.pack(side="left", padx=(6, 0))
        tk.Label(limit_frame, text="건을 넘으면 멈춤", fg="#5f6368").pack(side="left", padx=(6, 0))

        # --- 멈춘 실행 이어서 하기 ---
        # 실행 도중 마우스를 건드리면 샵마인 조작이 중단된다. 그때 처음부터
        # 다시 돌리면 제일 오래 걸리는 송장조회를 통째로 다시 하게 되므로,
        # 멈춘 자리부터 이어갈 수 있는 버튼을 따로 둔다.
        self.resume_button = tk.Button(
            root,
            text="↻  멈춘 지점부터 다시 시작",
            font=("맑은 고딕", 11),
            command=self.start_resume,
            state="disabled",
        )
        self.resume_button.pack(pady=(2, 0))
        self.resume_label = tk.Label(root, text="", fg="#5f6368", anchor="center")
        self.resume_label.pack(fill="x", padx=14, pady=(0, 4))

        tk.Frame(root, height=1, bg="#dadce0").pack(fill="x", padx=14, pady=6)

        # --- 파일만 만들기 (예전 방식) ---
        tk.Label(
            root,
            text="또는, 이미 받아둔 엑셀로 업로드 파일만 만들기",
            anchor="w",
            fg="#5f6368",
        ).pack(fill="x", padx=14)

        file_frame = tk.Frame(root)
        file_frame.pack(fill="x", padx=14, pady=(4, 0))
        self.file_label = tk.Label(file_frame, text=self._file_display(), anchor="w", fg="#1a73e8")
        self.file_label.pack(side="left", fill="x", expand=True)
        tk.Button(file_frame, text="다른 파일 선택...", command=self.choose_file).pack(side="right")

        self.run_button = tk.Button(
            root,
            text="▶  파일만 만들기",
            font=("맑은 고딕", 11),
            command=self.start_run,
        )
        self.run_button.pack(pady=6)

        self.log = tk.Text(root, height=14, state="disabled")
        self.log.pack(fill="both", expand=True, padx=14, pady=8)

        # 업로드용 파일(CSV)과 조회 결과 엑셀은 쓰임새가 달라 따로 연다.
        # 중간에 멈춘 실행에서도 조회 결과 엑셀은 만들어져 있다.
        button_frame = tk.Frame(root)
        button_frame.pack(pady=(0, 14))
        self.open_button = tk.Button(button_frame, text="업로드용 파일 열기",
                                     command=self.open_output, state="disabled")
        self.open_button.pack(side="left", padx=4)
        self.excel_button = tk.Button(button_frame, text="조회 결과 엑셀 열기",
                                      command=self.open_result_excel, state="disabled")
        self.excel_button.pack(side="left", padx=4)

        self._refresh_resume()
        self.root.after(200, self._poll_queue)

    def _file_display(self) -> str:
        if self.selected_file is None:
            return "(자동으로 찾은 파일이 없습니다 - 직접 선택해주세요)"
        return f"{self.selected_file.name}"

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="발송대상 엑셀 파일 선택",
            initialdir=str(DESKTOP),
            filetypes=[("Excel 파일", "*.xls *.xlsx"), ("모든 파일", "*.*")],
        )
        if path:
            self.selected_file = Path(path)
            self.file_label.config(text=self._file_display())

    def _log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _read_max_apply(self) -> int | None:
        try:
            return int(self.max_apply.get())
        except ValueError:
            messagebox.showerror("입력 오류", "최대 반영 건수는 숫자로 입력해주세요.")
            return None

    def start_full_auto(self) -> None:
        """1~8단계 전부 자동. 실제 주문 데이터가 바뀌므로 한 번 확인받는다."""
        max_apply = self._read_max_apply()
        if max_apply is None:
            return

        if not messagebox.askokcancel(
            "송장",
            "샵마인에서 주문 목록을 받아 송장을 조회하고, 쇼핑몰까지 반영합니다.\n\n"
            "· 먼저 쇼핑몰이 전부 연결돼 있는지 확인합니다 "
            "(끊긴 곳은 다시 연결합니다)\n"
            "· 샵마인이 다른 탭을 보고 있으면 [배송중] 탭으로 옮깁니다\n"
            "· 실행 중에는 마우스와 키보드를 건드리지 마세요\n"
            f"· 조회 성공이 {max_apply}건을 넘으면 반영하지 않고 멈춥니다\n\n"
            "진행할까요?",
        ):
            return

        self._set_busy(True, "처리 중...")
        self._log("전부 자동 처리를 시작합니다. 마우스를 건드리지 말아주세요.\n")
        threading.Thread(target=self._full_auto_worker, args=(max_apply, False),
                         daemon=True).start()

    def start_resume(self) -> None:
        """멈춘 실행을 이어서. 이미 끝낸 송장조회는 다시 하지 않는다."""
        max_apply = self._read_max_apply()
        if max_apply is None:
            return
        state = checkpoint.load()
        if state is None:
            messagebox.showinfo("이어서 할 작업 없음",
                                "이어서 할 진행 상황이 없습니다.\n"
                                "[송장]으로 처음부터 실행해주세요.")
            self._refresh_resume()
            return

        if not messagebox.askokcancel(
            "멈춘 지점부터 다시 시작",
            f"지난 실행이 멈춘 지점부터 이어서 합니다.\n\n"
            f"· {state.describe()}\n"
            "· 이미 내보낸 주문목록을 그대로 쓰고, 이미 조회한 주문은 건너뜁니다\n"
            "· 실행 중에는 마우스와 키보드를 건드리지 마세요\n\n"
            "진행할까요?",
        ):
            return

        self._set_busy(True, "이어서 처리 중...")
        self._log(f"멈춘 지점부터 이어서 시작합니다 ({state.describe()}).\n")
        threading.Thread(target=self._full_auto_worker, args=(max_apply, True),
                         daemon=True).start()

    def start_inquiry(self) -> None:
        """결과 엑셀의 '2일 지남' 주문에 1:1 문의를 남긴다. 실제로 글이 올라가므로 확인받는다."""
        try:
            path, targets, by_site, skipped = inquiry.plan()
        except Exception as e:  # noqa: BLE001 - 엑셀이 깨졌거나 시트가 다르면 여기서 알린다
            messagebox.showerror("엑셀 읽기 실패", str(e))
            return
        if path is None:
            messagebox.showerror("결과 엑셀 없음",
                                 "바탕화면에 '송장조회결과_*.xlsx' 파일이 없습니다.\n"
                                 "먼저 [송장]으로 송장 조회를 돌려주세요.")
            return
        to_post = sum(len(v) for v in by_site.values())
        site_lines = "\n".join(f"   · {site} {len(items)}건" for site, items in by_site.items())
        if to_post == 0:
            messagebox.showinfo(
                "문의할 주문 없음",
                f"{path.name}\n\n'{inquiry.STALE_SHEET_NAME}' 시트의 {inquiry.TARGET_DAYS_TEXT} 지남 "
                f"{len(targets)}건 중 남길 건이 없습니다.\n"
                f"(이미 남긴 주문이거나 아직 자동화하지 않은 사이트입니다 - {len(skipped)}건)")
            return
        if not messagebox.askokcancel(
            "문의 남기기",
            f"{path.name}\n\n"
            f"'{inquiry.STALE_SHEET_NAME}' 시트의 {inquiry.TARGET_DAYS_TEXT} 지남 {len(targets)}건 중\n"
            f"아래 {to_post}건에 \"(수령인) 배송 언제 시작하나요?\" 문의를 남깁니다.\n"
            f"{site_lines}\n"
            f"   · 넘김 {len(skipped)}건 (이미 남김 / 아직 지원하지 않는 사이트)\n\n"
            "· 공급사 사이트에 실제로 문의 글이 올라갑니다\n"
            "· 로그인이 필요하면 브라우저 창이 뜹니다\n\n"
            "진행할까요?",
        ):
            return
        self._set_busy(True, "문의 남기는 중...")
        self._log("문의 남기기를 시작합니다.\n")
        threading.Thread(target=self._inquiry_worker, args=(str(path),), daemon=True).start()

    def _inquiry_worker(self, excel_path: str) -> None:
        try:
            result = inquiry.run(excel_path, headless=False,
                                 log=lambda msg: self._queue.put(("log", msg)))
            # 남겼는지/못 남겼는지를 사람이 바로 열어볼 엑셀로 (바탕화면).
            inquiry.save_result_excel(result, log=lambda msg: self._queue.put(("log", msg)))
            inquiry.save_run_log(result)
            self._queue.put(("inquiry_done", result))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _full_auto_worker(self, max_apply: int, resume: bool) -> None:
        try:
            result = pipeline.run_full(
                max_apply=max_apply,
                resume=resume,
                log=lambda msg: self._queue.put(("log", msg)),
            )
            self._output_path = result.csv_path
            self._queue.put(("auto_done", result))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _refresh_resume(self) -> None:
        """이어서 할 진행 상황이 있는지 보고 [다시 시작] 버튼을 켜고 끈다."""
        state = checkpoint.load()
        if state is None:
            self.resume_button.config(state="disabled")
            self.resume_label.config(text="(멈춘 실행 없음)")
            return
        self.resume_button.config(state="normal")
        self.resume_label.config(text=f"멈춘 실행: {state.describe()}",
                                 fg="#d93025")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.auto_button.config(state=state,
                                text=text if busy else "⚡  송장")
        self.run_button.config(state=state,
                               text=text if busy else "▶  파일만 만들기")
        self.inquiry_button.config(state=state)
        if busy:
            self.resume_button.config(state="disabled")
            self.open_button.config(state="disabled")
            self.excel_button.config(state="disabled")
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.config(state="disabled")
        else:
            # 방금 끝난 실행이 멈췄으면 여기서 [다시 시작]이 다시 켜진다.
            self._refresh_resume()

    def start_run(self) -> None:
        if self.selected_file is None or not self.selected_file.exists():
            messagebox.showerror("파일 없음", "먼저 발송대상 엑셀 파일을 선택해주세요.")
            return

        self._set_busy(True, "처리 중...")
        self._log(f"입력 파일: {self.selected_file.name}")
        self._log("처리를 시작합니다. 롯데온 로그인이 필요하면 별도 브라우저 창이 뜹니다...\n")

        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        output_path = default_output_path()
        try:

            def on_progress(index: int, total: int, order_id: str, message: str) -> None:
                self._queue.put(("log", f"[{index}/{total}] {order_id}: {message}"))

            report = run_orchestrator(
                str(self.selected_file), str(output_path), headless=False, on_progress=on_progress
            )
            counts = report.summary()
            failure_lines = report.failure_lines()
            attention_blocks = report.attention_blocks()
            report_path = report.save()
            # 조회 결과는 JSON 말고 사람이 바로 열어볼 엑셀로도 남긴다.
            excel_path = result_excel.save_run_result(
                report.entries, counts,
                applied_label="미반영 (업로드 전)",
                paths=(("입력 엑셀", self.selected_file),
                       ("업로드용 파일", output_path if counts["success"] else None),
                       ("상세 로그", report_path)),
                log=lambda msg: self._queue.put(("log", msg)))
            self._queue.put(
                ("done", (counts, failure_lines, attention_blocks, output_path, excel_path)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    counts, failure_lines, attention_blocks, output_path, excel_path = payload
                    self._set_result_excel(excel_path)
                    self._log(f"\n완료: 성공 {counts['success']} / 실패 {counts['fail']} / 스킵 {counts['skip']}")
                    if failure_lines:
                        self._log("\n실패한 주문 (샵마인에서 직접 확인해주세요):")
                        for line in failure_lines:
                            self._log(line)
                    # 조회 자체를 못 한 주문(아직 지원하지 않는 사이트, 취소/품절)
                    for title, lines in attention_blocks:
                        self._log(f"\n{title}:")
                        for line in lines:
                            self._log(line)
                    if counts["success"] > 0:
                        self._output_path = output_path
                        self._log(f"업로드용 파일: {output_path}")
                        self._log("이 파일을 샵마인 [발송정보일괄등록(수정용)]으로 업로드해주세요.")
                        self.open_button.config(state="normal")
                    self._set_busy(False)
                elif kind == "auto_done":
                    result = payload
                    self._log("")
                    self._log("=" * 40)
                    self._log(pipeline.summarize(result))
                    self._log("=" * 40)
                    # 반영까지 못 가고 멈췄어도 조회 결과 엑셀은 열 수 있게 한다.
                    self._set_result_excel(result.result_excel_path)
                    if result.csv_path and Path(result.csv_path).exists():
                        self.open_button.config(state="normal")
                    self._set_busy(False)
                    if result.stopped_reason and not result.applied:
                        messagebox.showwarning("멈춤", result.stopped_reason)
                elif kind == "inquiry_done":
                    result = payload
                    self._log("")
                    self._log("=" * 40)
                    self._log(inquiry.summarize(result))
                    self._log("=" * 40)
                    self._set_result_excel(result.result_excel_path, label="문의 결과 엑셀 열기")
                    self._set_busy(False)
                    if result.stopped_reason:
                        messagebox.showwarning("멈춤", result.stopped_reason)
                elif kind == "error":
                    self._log(f"\n오류가 발생했습니다: {payload}")
                    self._set_busy(False)
                    messagebox.showerror("오류", str(payload))
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _set_result_excel(self, path, label: str = "조회 결과 엑셀 열기") -> None:
        """[엑셀 열기] 버튼이 열 파일. 송장조회 결과와 문의 결과가 같은 버튼을 쓴다."""
        self._result_excel_path = Path(path) if path else None
        if self._result_excel_path and self._result_excel_path.exists():
            self.excel_button.config(state="normal", text=label)

    def open_output(self) -> None:
        if self._output_path and Path(self._output_path).exists():
            os.startfile(self._output_path)  # noqa: S606 - 사용자가 방금 생성한 자신의 파일

    def open_result_excel(self) -> None:
        if self._result_excel_path and self._result_excel_path.exists():
            os.startfile(self._result_excel_path)  # noqa: S606 - 사용자가 방금 생성한 자신의 파일


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
