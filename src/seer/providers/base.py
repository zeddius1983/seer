from abc import ABC, abstractmethod
from typing import Iterator


class Provider(ABC):
    @abstractmethod
    def stream(self, system: str, prompt: str) -> Iterator[str]:
        """Yield response text chunks as they stream in."""
        ...
