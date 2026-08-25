from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import ReportEntry

LOG_DIR = Path("logs")


class RunReport:
    """한 번의 실행 결과(성공/실패/스킵)를 모아 리포트 파일로 저장한다.

    개인정보(고객명/주소/전화번호 등)는 절대 기록하지 않는다.
    """

    def __init__(self) -> None:
        self.entries: list[ReportEntry] = []

    def success(self, order_id: str, courier: str | None, tracking_no: str) -> None:
        self.entries.append(
            ReportEntry(order_id=order_id, status="success", courier=courier, tracking_no=tracking_no)
        )

    def fail(self, order_id: str, reason: str) -> None:
        self.entries.append(ReportEntry(order_id=order_id, status="fail", reason=reason))

    def skip(self, order_id: str, reason: str) -> None:
        self.entries.append(ReportEntry(order_id=order_id, status="skip", reason=reason))

    def summary(self) -> dict[str, int]:
        counts = {"success": 0, "fail": 0, "skip": 0}
        for entry in self.entries:
            counts[entry.status] += 1
        return counts

    def save(self) -> Path:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json"
        payload = [entry.model_dump() for entry in self.entries]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
