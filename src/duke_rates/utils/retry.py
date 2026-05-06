from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_call(
    func: Callable[[], T],
    *,
    retries: int,
    delay_seconds: float,
    retry_on: tuple[type[BaseException], ...],
) -> T:
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return func()
        except retry_on as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(delay_seconds * (attempt + 1))
    assert last_error is not None
    raise last_error
