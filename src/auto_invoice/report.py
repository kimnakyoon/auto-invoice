from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import ReportEntry

LOG_DIR = Path("logs")


class RunReport:
    """한 번의 실행 결과(성공/실패/스킵)를 모아 리포트 파일로 저장한다.

    개인정보(고객명/주소/전화번호 등)는 원칙적으로 기록하지 않는다. 유일한
    예외는 실패 건의 수령인 이름이다 - 사람이 샵마인에서 그 주문을 직접 찾아
    확인해야 하는데, 마켓 주문번호만으로는 찾기 번거롭다는 요청이 있어
    fail()에 한해서만 남긴다 (success/skip에는 남기지 않는다).
    """

    def __init__(self) -> None:
        self.entries: list[ReportEntry] = []

    def success(self, order_id: str, courier: str | None, tracking_no: str) -> None:
        self.entries.append(
            ReportEntry(order_id=order_id, status="success", courier=courier, tracking_no=tracking_no)
        )

    def fail(self, order_id: str, reason: str, recipient_name: str | None = None) -> None:
        self.entries.append(
            ReportEntry(order_id=order_id, status="fail", reason=reason, recipient_name=recipient_name or None)
        )

    def exclude(self, order_id: str, reason: str, recipient_name: str | None = None) -> None:
        """조회는 성공했지만 자동 반영에서 뺀 주문 (사람이 직접 처리해야 한다).

        성공 기록을 지우고 실패로 다시 남긴다 - 성공 건수가 곧 '업로드 대상'
        이어야 뒤따르는 건수 검증이 맞아떨어지고, 실패 목록에 남아야 사람이
        놓치지 않는다.
        """
        self.entries = [
            e for e in self.entries if not (e.order_id == order_id and e.status == "success")
        ]
        self.fail(order_id, reason, recipient_name=recipient_name)

    def skip(self, order_id: str, reason: str) -> None:
        self.entries.append(ReportEntry(order_id=order_id, status="skip", reason=reason))

    def failures(self) -> list[ReportEntry]:
        return [entry for entry in self.entries if entry.status == "fail"]

    def failure_lines(self) -> list[str]:
        """실패 건을 '주문번호 (수령인: 이름) - 사유' 한 줄씩으로 정리한다.

        실행이 끝난 뒤 사람이 샵마인에서 해당 주문을 직접 찾아 처리해야 하므로
        주문번호만이 아니라 수령인 이름도 같이 보여준다.
        """
        lines = []
        for entry in self.failures():
            who = f" (수령인: {entry.recipient_name})" if entry.recipient_name else ""
            lines.append(f"  - {entry.order_id}{who}: {entry.reason}")
        return lines

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
