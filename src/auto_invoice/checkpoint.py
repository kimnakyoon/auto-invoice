"""실행이 중간에 멈췄을 때 '멈춘 지점부터 다시 시작'하기 위한 진행 상황 파일.

왜 필요한가: 실행 중에 사람이 마우스를 움직이거나 다른 창을 앞으로 가져오면
샵마인 조작이 중단된다(winui.move_click / grid._click_checkbox 참고). 그런데
가장 오래 걸리는 5단계 송장조회는 한 건에 수십 초씩 걸려서, 처음부터 다시
돌리면 이미 끝낸 조회를 전부 다시 하게 된다. 실제로 그게 제일 아깝다.

그래서 주문 한 건을 조회할 때마다 지금까지의 결과를 이 파일에 적어둔다.
[다시 시작]을 누르면

  - 이미 내보낸 주문목록 엑셀을 그대로 쓰고 (3~4단계 건너뜀)
  - 이미 조회한 주문은 공급사에 다시 묻지 않고 (5단계 이어서)
  - 남은 주문만 조회한 뒤 6~8단계로 넘어간다

저장하는 결과는 '중복 주문 정리(orchestrator._drop_split_orders) 전'의 값이다.
정리는 조회가 다 끝난 뒤 전체를 놓고 한 번에 하는 판단이라, 정리된 값을
저장해두면 이어서 시작할 때 같은 판단이 두 번 적용된다.

파일 하나만 쓴다(logs/checkpoint.json). 동시에 두 번 실행할 일이 없고,
'마지막으로 멈춘 실행'만 이어서 하면 되기 때문이다. 끝까지 성공하면 지운다 -
남아 있으면 [다시 시작]이 이미 끝난 작업을 가리키게 된다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import ReportEntry
from .report import LOG_DIR

PATH = LOG_DIR / "checkpoint.json"

VERSION = 1


class Checkpoint:
    """멈춘 실행의 진행 상황. 없는 값은 전부 비어 있는 채로 둔다."""

    def __init__(self, data: dict) -> None:
        self.export_path: str = data.get("export_path") or ""
        self.csv_path: str = data.get("csv_path") or ""
        self.updated_at: str = data.get("updated_at") or ""
        self.entries: list[ReportEntry] = [
            ReportEntry.model_validate(e) for e in data.get("entries") or []
        ]
        # (주문번호, 송장번호, 택배사) - JSON에는 리스트로 저장된다.
        self.rows: list[tuple] = [tuple(r) for r in data.get("rows") or []]

    @property
    def looked_up(self) -> int:
        return len(self.entries)

    def export_file(self) -> Path | None:
        """이어서 쓸 수 있는 내보내기 엑셀 (없거나 지워졌으면 None)."""
        if not self.export_path:
            return None
        path = Path(self.export_path)
        return path if path.exists() else None

    def describe(self) -> str:
        """[다시 시작]을 누르기 전에 사람에게 보여줄 한 줄."""
        when = self.updated_at or "(시각 모름)"
        where = Path(self.export_path).name if self.export_path else "(주문목록 없음)"
        return f"{when} 실행 / 조회 끝낸 주문 {self.looked_up}건 / 주문목록 {where}"


def save(*, export_path=None, csv_path=None, entries=None, rows=None) -> None:
    """진행 상황을 덮어쓴다. 넘기지 않은 항목은 파일에 있던 값을 유지한다.

    저장에 실패해도 실행 자체를 멈추지 않는다 - 이어서 하기 위한 편의일 뿐이라,
    못 남겼다고 지금 하던 조회를 버릴 이유가 없다.
    """
    current = _read() or {}
    data = {
        "version": VERSION,
        "updated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "export_path": _str_or(export_path, current.get("export_path")),
        "csv_path": _str_or(csv_path, current.get("csv_path")),
        "entries": ([e.model_dump(mode="json") for e in entries]
                    if entries is not None else current.get("entries") or []),
        "rows": ([list(r) for r in rows]
                 if rows is not None else current.get("rows") or []),
    }
    try:
        LOG_DIR.mkdir(exist_ok=True)
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 진행 상황 저장 실패가 실행을 덮으면 안 된다
        pass


def load() -> Checkpoint | None:
    """이어서 할 진행 상황. 없거나 읽을 수 없으면 None."""
    data = _read()
    if not data or data.get("version") != VERSION:
        return None
    found = Checkpoint(data)
    # 조회 결과도 없고 내보낸 엑셀도 없으면 이어서 할 게 없다.
    if not found.entries and found.export_file() is None:
        return None
    return found


def clear() -> None:
    """진행 상황을 지운다 (끝까지 마친 실행 뒤)."""
    try:
        PATH.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 뒷정리 실패가 실행 결과를 덮으면 안 된다
        pass


def _read() -> dict | None:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 파일이 없거나 깨졌으면 그냥 처음부터
        return None


def _str_or(value, fallback) -> str:
    if value is not None:
        return str(value)
    return fallback or ""
