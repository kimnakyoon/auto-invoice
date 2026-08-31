import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    delay_min: float
    delay_max: float
    workers: int
    lotteon_id: str | None
    gmarket_id: str | None
    ssg_id: str | None
    ssg_pw: str | None
    fashionplus_id: str | None


def load_settings() -> Settings:
    return Settings(
        delay_min=float(os.environ.get("AUTO_INVOICE_DELAY_MIN", "1.5")),
        delay_max=float(os.environ.get("AUTO_INVOICE_DELAY_MAX", "4.0")),
        # 동시에 조회할 공급사 수. 사이트끼리는 서로 상관이 없어 같이 돌려도
        # 되지만, 그만큼 브라우저를 동시에 띄우므로 무한정 늘리지는 않는다.
        # 1로 두면 예전처럼 한 사이트씩 순서대로 돈다.
        workers=max(1, int(os.environ.get("AUTO_INVOICE_WORKERS", "4"))),
        lotteon_id=os.environ.get("LOTTEON_ID"),
        gmarket_id=os.environ.get("GMARKET_ID"),
        ssg_id=os.environ.get("SSG_ID"),
        ssg_pw=os.environ.get("SSG_PW"),
        fashionplus_id=os.environ.get("FASHIONPLUS_ID"),
    )
