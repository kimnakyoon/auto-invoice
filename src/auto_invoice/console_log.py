"""실행 중 콘솔에 찍히는 것을 logs/console_<시작시각>.log 에도 남긴다.

조회 결과는 logs/run_*.json 에 남지만, 공급사 어댑터가 도중에 print로 찍는
말("[gsshop] 체크박스를 눌러주세요" 같은 것)은 콘솔에만 나오고 사라졌다. 그래서
나중에 '왜 그 건은 사람 손을 탔나'를 되짚을 수 없었다 (2026-09-04, 09-05).

두 진입점이 같은 파일 형식을 쓴다:
  - 터미널(run_all.py): sys.stdout/stderr 를 Tee 로 바꿔 콘솔과 파일에 같이 쓴다.
  - GUI(gui.pyw): pythonw 라 stdout 이 아예 없다. sys.stdout/stderr 를 LineWriter
    로 바꿔 print() 한 줄이 로그창으로 가게 하고, 로그창이 파일에 쓴다.
    어댑터 안내문과 트레이스백이 GUI에서 통째로 사라지던 것을 이렇게 막는다.
"""

from __future__ import annotations

import threading
from datetime import datetime

from .report import LOG_DIR


def open_log(started: datetime | None = None):
    """이번 실행의 로그 파일 (못 만들면 None - 로그 때문에 실행이 깨지면 안 된다)."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"console_{started or datetime.now():%Y%m%d_%H%M%S}.log"
        return open(path, "a", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


class Tee:
    """stream 에 쓰는 것을 file 에도 그대로 쓴다 (sys.stdout 자리에 둔다)."""

    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

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


class LineWriter:
    """print() 된 글을 줄 단위로 on_line 에 넘긴다 (sys.stdout 자리에 둔다).

    작업 스레드 여러 개가 동시에 print 해도 줄이 섞이지 않게 잠근다.
    passthrough 가 있으면(콘솔에서 띄운 GUI) 거기에도 그대로 쓴다.
    """

    encoding = "utf-8"

    def __init__(self, on_line, passthrough=None):
        self._on_line = on_line
        self._passthrough = passthrough
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text):
        if self._passthrough is not None:
            try:
                self._passthrough.write(text)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._on_line(line)

    def flush(self):
        if self._passthrough is not None:
            try:
                self._passthrough.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False
