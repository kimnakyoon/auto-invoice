from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import AttentionCategory, ReportEntry

LOG_DIR = Path("logs")

# 실행이 끝난 뒤 사람이 따로 챙겨야 하는 주문들. 일반 실패 목록과 섞이면
# 묻혀서, 마지막 결과 정리에 이 분류별로 따로 묶어 보여준다.
ATTENTION_TITLES: dict[AttentionCategory, str] = {
    "unsupported_site": "아직 지원하지 않는 사이트 (직접 조회해주세요)",
    "cancelled": "취소/품절로 보이는 주문 (직접 확인해주세요)",
}


class RunReport:
    """한 번의 실행 결과(성공/실패/스킵)를 모아 리포트 파일로 저장한다.

    개인정보(고객명/주소/전화번호 등)는 원칙적으로 기록하지 않는다. 예외는
    사람이 직접 손봐야 하는 건 - 실패 건과 ATTENTION_TITLES로 분류한 건 -
    의 수령인 이름이다. 샵마인에서 그 주문을 찾아야 하는데 마켓 주문번호
    만으로는 번거롭다는 요청이 있어서다. 그 외 성공/일반 스킵에는 남기지 않는다.
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

    def skip(self, order_id: str, reason: str, recipient_name: str | None = None,
             category: AttentionCategory | None = None) -> None:
        self.entries.append(
            ReportEntry(
                order_id=order_id,
                status="skip",
                reason=reason,
                recipient_name=recipient_name or None,
                category=category,
            )
        )

    def unsupported_site(self, order_id: str, product_url: str,
                         recipient_name: str | None = None) -> None:
        """상품URL의 도메인에 맞는 공급사 어댑터가 아직 없는 주문.

        오류가 아니라 '이 사이트는 아직 안 만들었다'는 뜻이라 스킵으로 세되,
        사람이 그 주문만 직접 조회해야 하므로 따로 모아 보여준다.
        """
        self.skip(order_id, f"등록된 어댑터 없음: {product_url}",
                  recipient_name=recipient_name, category="unsupported_site")

    def cancelled(self, order_id: str, reason: str,
                  recipient_name: str | None = None) -> None:
        """공급사 화면에 취소/품절 표시가 있어 송장이 나올 수 없는 주문.

        '아직 미발급' 스킵과 달리 기다려도 해결되지 않아서(suppliers/base.py의
        OrderCancelled 참고) 따로 모아 사람에게 넘긴다.
        """
        self.skip(order_id, reason, recipient_name=recipient_name, category="cancelled")

    def failures(self) -> list[ReportEntry]:
        return [entry for entry in self.entries if entry.status == "fail"]

    def failure_lines(self) -> list[str]:
        """실패 건을 '주문번호 (수령인: 이름) - 사유' 한 줄씩으로 정리한다.

        실행이 끝난 뒤 사람이 샵마인에서 해당 주문을 직접 찾아 처리해야 하므로
        주문번호만이 아니라 수령인 이름도 같이 보여준다.
        """
        return [_entry_line(entry) for entry in self.failures()]

    def attention_entries(self, category: AttentionCategory) -> list[ReportEntry]:
        return [e for e in self.entries if e.category == category]

    def attention_blocks(self) -> list[tuple[str, list[str]]]:
        """사람이 따로 챙겨야 하는 주문을 분류별로 (제목, 줄 목록)으로 묶는다.

        실패 목록과 같은 형식('주문번호 (수령인: 이름) - 사유')이라 결과
        정리에 이어 붙이기만 하면 된다. 해당 건이 없는 분류는 아예 뺀다.
        """
        blocks = []
        for category, title in ATTENTION_TITLES.items():
            entries = self.attention_entries(category)
            if entries:
                blocks.append((title, [_entry_line(e) for e in entries]))
        return blocks

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


def _entry_line(entry: ReportEntry) -> str:
    """'  - 주문번호 (수령인: 이름): 사유' 한 줄.

    사람이 샵마인에서 해당 주문을 직접 찾아 처리해야 하므로 주문번호만이
    아니라 수령인 이름도 같이 보여준다.
    """
    who = f" (수령인: {entry.recipient_name})" if entry.recipient_name else ""
    return f"  - {entry.order_id}{who}: {entry.reason}"
