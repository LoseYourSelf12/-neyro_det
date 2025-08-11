from collections import deque
from typing import Deque

class CountsAggregator:
    """Aggregate vehicle counts over multiple frames."""

    def __init__(self) -> None:
        self._counts: Deque[int] = deque()

    def add(self, count: int) -> None:
        """Add vehicle count from a single frame."""
        self._counts.append(count)

    def average(self) -> float:
        """Return the average count of vehicles."""
        if not self._counts:
            return 0.0
        return sum(self._counts) / len(self._counts)

    def clear(self) -> None:
        """Reset stored counts."""
        self._counts.clear()
