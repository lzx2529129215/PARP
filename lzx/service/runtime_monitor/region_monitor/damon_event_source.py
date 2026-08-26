from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from .models import DamonEvent


class DamonEventSource(ABC):
    @abstractmethod
    def events(self) -> Iterator[DamonEvent]:
        raise NotImplementedError

    def close(self) -> None:
        return None

