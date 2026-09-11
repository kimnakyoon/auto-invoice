from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class PendingOrder(BaseModel):
    """샵마인에서 송장번호가 비어있는 주문 한 건."""

    order_id: str
    product_url: str = ""
    recipient_name: str = ""
    # 한 공급사 주문 안에 상품별로 송장번호가 여러 개 있을 때, 이 주문의
    # 상품(색상/사이즈 등 옵션)을 식별해서 맞는 송장을 고르는 데 쓴다.
    order_option: str = ""


class TrackingResult(BaseModel):
    """공급사 어댑터가 반환하는 송장 조회 결과."""

    tracking_no: str
    courier: Optional[str] = None
    note: Optional[str] = None
    # 공급사 주문상세에 적혀 있던 주문일. 오늘과 이틀 이상 벌어진 주문은
    # 결과 정리에서 따로 모아 보여준다 (order_date.py 참고). 화면에서
    # 읽지 못했으면 None이고, 그러면 아무 표시도 하지 않는다.
    order_date: Optional[date] = None
    # 주문상세에 적혀 있던 '출고예정 2026-09-02' 같은 안내 문구 (eta.py).
    # 주문일이 오래된 건을 사람이 판단할 때 쓰라고 같이 실어 보낸다.
    delivery_note: Optional[str] = None
    # 이 결과를 내기까지 공급사 사이트에 요청을 보냈는가. 미리 읽어둔
    # 주문목록만으로 답한 경우(29CM prepare_batch)만 False다 - 예외 쪽의
    # AdapterError.sent_request와 같은 뜻이고, 오케스트레이터가 요청 간격을
    # 지킬지 판단하는 데 쓴다 (요청을 안 보냈으면 간격을 둘 이유도 없다).
    sent_request: bool = True


# 실행이 끝난 뒤 사람이 따로 챙겨야 하는 주문의 종류. 마지막 결과 정리에서
# 이 값별로 묶어서 보여준다 (report.ATTENTION_TITLES 참고).
AttentionCategory = Literal["unsupported_site", "cancelled", "apply_error"]


class ReportEntry(BaseModel):
    order_id: str
    status: Literal["success", "fail", "skip"]
    reason: Optional[str] = None
    courier: Optional[str] = None
    tracking_no: Optional[str] = None
    # 사람이 샵마인에서 직접 찾아 확인해야 하는 건(실패 + 아래 category가
    # 붙은 건)에만 수령인 이름을 남긴다. 그 외 성공/스킵 건에는 남기지
    # 않는다 - 개인정보 최소 기록 원칙.
    recipient_name: Optional[str] = None
    # 사람 손이 필요한 이유를 분류해둔다. None이면 일반 성공/실패/스킵.
    category: Optional[AttentionCategory] = None
    # 공급사 주문상세에서 읽은 주문일 (못 읽었으면 None). 성공/실패/스킵과
    # 상관없이 오래된 주문을 따로 뽑는 데 쓴다 - order_date.py 참고.
    order_date: Optional[date] = None
    # 주문상세에 적혀 있던 출고/도착 예정 문구 (eta.py). 주문일이 오래된 건은
    # 이 문구가 '기다리면 되는 건지'를 판단하는 근거라 결과 엑셀에 같이 낸다.
    delivery_note: Optional[str] = None
    # 샵마인 엑셀의 상품URL. 주문일이 오래된 건은 사람이 그 화면을 직접 열어
    # 확인해야 해서 결과 엑셀에 링크로 같이 낸다.
    product_url: Optional[str] = None
