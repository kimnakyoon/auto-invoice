"""요청 사이에 사람처럼 보이는 랜덤 대기를 넣어 봇 탐지(Imperva 등)를 피한다."""

import random
import time


def humanized_delay(min_s: float = 1.5, max_s: float = 4.0) -> None:
    time.sleep(random.uniform(min_s, max_s))
