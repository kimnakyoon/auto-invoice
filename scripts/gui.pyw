"""송장 자동화 - 더블클릭으로 실행하는 간단한 창.

바탕화면의 최근 엑셀 파일을 자동으로 찾아 보여주고, [실행] 버튼 하나로
샵마인 '발송정보일괄등록(수정용)' 업로드용 파일을 생성한다.
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

from auto_invoice.orchestrator import run as run_orchestrator  # noqa: E402

DESKTOP = Path.home() / "Desktop"

WINDOW_WIDTH = 600
WINDOW_HEIGHT = 460
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
        root.minsize(520, 400)

        self.selected_file: Path | None = find_latest_export()
        self._output_path: Path | None = None
        self._queue: "queue.Queue" = queue.Queue()

        tk.Label(
            root,
            text="1. 샵마인 [주문관리 > 발송대상 > 엑셀파일생성]으로 받은 파일",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 2))

        file_frame = tk.Frame(root)
        file_frame.pack(fill="x", padx=14)
        self.file_label = tk.Label(file_frame, text=self._file_display(), anchor="w", fg="#1a73e8")
        self.file_label.pack(side="left", fill="x", expand=True)
        tk.Button(file_frame, text="다른 파일 선택...", command=self.choose_file).pack(side="right")

        tk.Label(
            root,
            text="2. 아래 버튼을 누르면 송장번호를 자동으로 조회합니다",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 4))

        self.run_button = tk.Button(
            root,
            text="▶  실행",
            font=("맑은 고딕", 14, "bold"),
            bg="#1a73e8",
            fg="white",
            activebackground="#1558b0",
            activeforeground="white",
            command=self.start_run,
        )
        self.run_button.pack(pady=6)

        self.log = tk.Text(root, height=14, state="disabled")
        self.log.pack(fill="both", expand=True, padx=14, pady=8)

        self.open_button = tk.Button(root, text="결과 파일 열기", command=self.open_output, state="disabled")
        self.open_button.pack(pady=(0, 14))

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

    def start_run(self) -> None:
        if self.selected_file is None or not self.selected_file.exists():
            messagebox.showerror("파일 없음", "먼저 발송대상 엑셀 파일을 선택해주세요.")
            return

        self.run_button.config(state="disabled", text="처리 중...")
        self.open_button.config(state="disabled")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
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
            report.save()
            self._queue.put(("done", (counts, output_path)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    counts, output_path = payload
                    self._log(f"\n완료: 성공 {counts['success']} / 실패 {counts['fail']} / 스킵 {counts['skip']}")
                    if counts["success"] > 0:
                        self._output_path = output_path
                        self._log(f"업로드용 파일: {output_path}")
                        self._log("이 파일을 샵마인 [발송정보일괄등록(수정용)]으로 업로드해주세요.")
                        self.open_button.config(state="normal")
                    self.run_button.config(state="normal", text="▶  실행")
                elif kind == "error":
                    self._log(f"\n오류가 발생했습니다: {payload}")
                    self.run_button.config(state="normal", text="▶  실행")
                    messagebox.showerror("오류", str(payload))
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def open_output(self) -> None:
        if self._output_path and self._output_path.exists():
            os.startfile(self._output_path)  # noqa: S606 - 사용자가 방금 생성한 자신의 파일


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
