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


class ReportEntry(BaseModel):
    order_id: str
    status: Literal["success", "fail", "skip"]
    reason: Optional[str] = None
    courier: Optional[str] = None
    tracking_no: Optional[str] = None
    # 실패 건만 사람이 샵마인에서 직접 찾아 확인하기 쉽도록 수령인 이름을
    # 남긴다 (성공/스킵 건에는 남기지 않는다 - 개인정보 최소 기록 원칙).
    recipient_name: Optional[str] = None
