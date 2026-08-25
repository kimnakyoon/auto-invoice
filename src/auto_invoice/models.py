from typing import Literal, Optional

from pydantic import BaseModel


class PendingOrder(BaseModel):
    """샵마인에서 송장번호가 비어있는 주문 한 건."""

    order_id: str
    product_url: str = ""


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
