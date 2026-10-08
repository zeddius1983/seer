from abc import ABC, abstractmethod
from typing import Iterator, Optional


class Provider(ABC):
    # The model that actually answered, when it differs from the configured
    # one (e.g. Claude Code resolving the `sonnet` alias). Set while streaming.
    resolved_model: Optional[str] = None

    @abstractmethod
    def stream(self, system: str, prompt: str) -> Iterator[str]:
        """Yield response text chunks as they stream in."""
        ...
