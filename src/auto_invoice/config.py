import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    delay_min: float
    delay_max: float
    lotteon_id: str | None
    gmarket_id: str | None


def load_settings() -> Settings:
    return Settings(
        delay_min=float(os.environ.get("AUTO_INVOICE_DELAY_MIN", "1.5")),
        delay_max=float(os.environ.get("AUTO_INVOICE_DELAY_MAX", "4.0")),
        lotteon_id=os.environ.get("LOTTEON_ID"),
        gmarket_id=os.environ.get("GMARKET_ID"),
    )
