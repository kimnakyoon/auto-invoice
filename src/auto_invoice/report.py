from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from . import order_date as order_date_mod
from .models import AttentionCategory, ReportEntry

LOG_DIR = Path("logs")

# 실행이 끝난 뒤 사람이 따로 챙겨야 하는 주문들. 일반 실패 목록과 섞이면
# 묻혀서, 마지막 결과 정리에 이 분류별로 따로 묶어 보여준다.
ATTENTION_TITLES: dict[AttentionCategory, str] = {
    "cancelled": "취소/품절로 보이는 주문 (직접 확인해주세요)",
    "unsupported_site": "아직 지원하지 않는 사이트 (직접 조회해주세요)",
}

# 주문일이 오래된 주문은 위 분류와 달리 성공/실패/스킵 어디에나 걸쳐 있어서
# (송장은 받았는데 주문일이 나흘 전일 수도 있다) category가 아니라 주문일로
# 따로 뽑는다. 결과 정리에서는 같은 자리에 같은 형식으로 붙인다.
STALE_TITLE = (f"주문일이 {order_date_mod.STALE_DAYS}일 이상 지난 주문 "
               "(발송이 늦어지는지 확인해주세요)")

# 결과 한 마디. 사람이 직접 처리해야 하는 스킵(미지원 사이트/취소·품절)은
# 그냥 '스킵'과 구분해서 보여준다 - 기다리면 되는 건과 손을 대야 하는 건이
# 다르다. 결과 엑셀(result_excel.py)도 같은 이름을 쓴다.
STATUS_LABELS = {"success": "성공", "fail": "실패", "skip": "스킵"}
CATEGORY_LABELS = {"unsupported_site": "미지원 사이트", "cancelled": "취소/품절"}


def result_label(entry: ReportEntry) -> str:
    if entry.category:
        return CATEGORY_LABELS.get(entry.category, entry.category)
    return STATUS_LABELS.get(entry.status, entry.status)


def is_stale_entry(entry: ReportEntry) -> bool:
    """주문일이 오래됐다고 따로 알려야 하는 건인가.

    '그냥 넘긴 스킵'에만 붙인다(사용자 요청) - 아직 송장이 안 나와서 다음
    실행에 다시 볼 건들이다. 사람이 알아야 하는 건 '아직 안 나갔는데 주문한
    지 며칠 됐다'는 것뿐이라, 나머지는 이 목록에 넣지 않는다:

      - 성공: 송장을 받아 이미 나가는 중이다.
      - 실패: 주문일이 며칠 됐든 실패 사유부터 풀어야 한다.
      - 취소/품절(category): 기다려도 안 나오는 건이라 '늦어지는 중'이
        아니고, ATTENTION_TITLES에 자기 목록이 이미 있다.
      - 미지원 사이트(category): 조회 자체를 안 해서 주문일도 없다.
    """
    return (entry.status == "skip" and entry.category is None
            and order_date_mod.is_stale(entry.order_date))


def stale_entries(entries: list[ReportEntry]) -> list[ReportEntry]:
    """주문일이 오래된 스킵 건만, 오래된 순서로."""
    return sorted((e for e in entries if is_stale_entry(e)),
                  key=lambda e: e.order_date)


class RunReport:
    """한 번의 실행 결과(성공/실패/스킵)를 모아 리포트 파일로 저장한다.

    개인정보(고객명/주소/전화번호 등)는 원칙적으로 기록하지 않는다. 예외는
    사람이 직접 손봐야 하는 건 - 실패 건, ATTENTION_TITLES로 분류한 건,
    주문일이 오래된 스킵 건(_name_if_needed) - 의 수령인 이름이다. 샵마인에서 그
    주문을 찾아야 하는데 마켓 주문번호만으로는 번거롭다는 요청이 있어서다.
    그 외 성공/일반 스킵에는 남기지 않는다.
    """

    def __init__(self) -> None:
        self.entries: list[ReportEntry] = []

    def success(self, order_id: str, courier: str | None, tracking_no: str,
                order_date: date | None = None) -> None:
        self.entries.append(
            ReportEntry(order_id=order_id, status="success", courier=courier,
                        tracking_no=tracking_no, order_date=order_date)
        )

    def fail(self, order_id: str, reason: str, recipient_name: str | None = None,
             order_date: date | None = None) -> None:
        self.entries.append(
            ReportEntry(order_id=order_id, status="fail", reason=reason,
                        recipient_name=recipient_name or None, order_date=order_date)
        )

    def exclude(self, order_id: str, reason: str, recipient_name: str | None = None) -> None:
        """조회는 성공했지만 자동 반영에서 뺀 주문 (사람이 직접 처리해야 한다).

        성공 기록을 지우고 실패로 다시 남긴다 - 성공 건수가 곧 '업로드 대상'
        이어야 뒤따르는 건수 검증이 맞아떨어지고, 실패 목록에 남아야 사람이
        놓치지 않는다. 지우는 성공 기록에 주문일이 있으면 그대로 옮겨온다.
        """
        removed = [
            e for e in self.entries if e.order_id == order_id and e.status == "success"
        ]
        self.entries = [e for e in self.entries if e not in removed]
        order_date = next((e.order_date for e in removed if e.order_date), None)
        self.fail(order_id, reason, recipient_name=recipient_name, order_date=order_date)

    def skip(self, order_id: str, reason: str, recipient_name: str | None = None,
             category: AttentionCategory | None = None,
             order_date: date | None = None) -> None:
        self.entries.append(
            ReportEntry(
                order_id=order_id,
                status="skip",
                reason=reason,
                recipient_name=(recipient_name or None) if category
                                else _name_if_needed(recipient_name, order_date),
                category=category,
                order_date=order_date,
            )
        )

    def unsupported_site(self, order_id: str, product_url: str,
                         recipient_name: str | None = None,
                         order_date: date | None = None) -> None:
        """상품URL의 도메인에 맞는 공급사 어댑터가 아직 없는 주문.

        오류가 아니라 '이 사이트는 아직 안 만들었다'는 뜻이라 스킵으로 세되,
        사람이 그 주문만 직접 조회해야 하므로 따로 모아 보여준다.
        """
        self.skip(order_id, f"등록된 어댑터 없음: {product_url}",
                  recipient_name=recipient_name, category="unsupported_site",
                  order_date=order_date)

    def cancelled(self, order_id: str, reason: str,
                  recipient_name: str | None = None,
                  order_date: date | None = None) -> None:
        """공급사 화면에 취소/품절 표시가 있어 송장이 나올 수 없는 주문.

        '아직 미발급' 스킵과 달리 기다려도 해결되지 않아서(suppliers/base.py의
        OrderCancelled 참고) 따로 모아 사람에게 넘긴다.
        """
        self.skip(order_id, reason, recipient_name=recipient_name, category="cancelled",
                  order_date=order_date)

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

    def stale_entries(self) -> list[ReportEntry]:
        return stale_entries(self.entries)

    def stale_lines(self) -> list[str]:
        return [_stale_line(e) for e in self.stale_entries()]

    def attention_blocks(self) -> list[tuple[str, list[str]]]:
        """사람이 따로 챙겨야 하는 주문을 분류별로 (제목, 줄 목록)으로 묶는다.

        실패 목록과 같은 형식('주문번호 (수령인: 이름) - 사유')이라 결과
        정리에 이어 붙이기만 하면 된다. 해당 건이 없는 분류는 아예 뺀다.

        순서는 결과 엑셀의 목록 순서(result_excel._SORT_ORDER)와 맞춰뒀다 -
        취소/품절 -> 주문일지연 -> 미지원 사이트. 그 앞의 '실패'는 이
        함수가 아니라 실패 목록이 따로 보여준다.

        '주문일이 오래된 주문'은 그냥 넘긴 스킵만 모으므로(is_stale_entry)
        세 목록에 같은 주문이 두 번 나오는 일은 없다.
        """
        blocks = [
            (ATTENTION_TITLES["cancelled"], self._attention_lines("cancelled")),
            (STALE_TITLE, self.stale_lines()),
            (ATTENTION_TITLES["unsupported_site"],
             self._attention_lines("unsupported_site")),
        ]
        return [(title, lines) for title, lines in blocks if lines]

    def _attention_lines(self, category: AttentionCategory) -> list[str]:
        return [_entry_line(e) for e in self.attention_entries(category)]

    def summary(self) -> dict[str, int]:
        counts = {"success": 0, "fail": 0, "skip": 0}
        for entry in self.entries:
            counts[entry.status] += 1
        return counts

    def save(self) -> Path:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json"
        # mode="json" 이라야 주문일(date)이 "2026-08-26" 문자열로 저장된다.
        payload = [entry.model_dump(mode="json") for entry in self.entries]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _name_if_needed(recipient_name: str | None, order_date: date | None) -> str | None:
    """일반 스킵 건의 수령인 이름은 주문일이 오래됐을 때만 남긴다.

    그냥 넘긴 스킵은 원래 사람이 볼 일이 없어서 이름을 기록하지 않는 게
    원칙인데(개인정보 최소 기록), 주문일이 오래된 건만은 사람이 샵마인에서
    찾아 확인해야 해서 예외로 둔다 - 마켓 주문번호만으로 찾기가 번거롭다.
    """
    return (recipient_name or None) if order_date_mod.is_stale(order_date) else None


def _entry_line(entry: ReportEntry) -> str:
    """'  - 주문번호 (수령인: 이름): 사유' 한 줄.

    사람이 샵마인에서 해당 주문을 직접 찾아 처리해야 하므로 주문번호만이
    아니라 수령인 이름도 같이 보여준다.
    """
    who = f" (수령인: {entry.recipient_name})" if entry.recipient_name else ""
    return f"  - {entry.order_id}{who}: {entry.reason}"


def _stale_line(entry: ReportEntry) -> str:
    """'  - 주문번호 (수령인: 이름): 주문일 2026-08-26 (2일 지남) - 조회결과 성공'.

    조회 결과까지 같이 보여준다 - 같은 '오래된 주문'이라도 송장을 받은 건과
    아직 못 받은 건은 사람이 할 일이 다르다.
    """
    who = f" (수령인: {entry.recipient_name})" if entry.recipient_name else ""
    return (f"  - {entry.order_id}{who}: 주문일 {order_date_mod.describe(entry.order_date)}"
            f" - 조회결과 {result_label(entry)}")
