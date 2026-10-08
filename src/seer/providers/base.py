from abc import ABC, abstractmethod
from typing import Iterator, Optional


class Provider(ABC):
    # The model that actually answered, when it differs from the configured
    # one (e.g. Claude Code resolving the `sonnet` alias). Set while streaming.
    resolved_model: Optional[str] = None
    # Reasoning the model streams before (or between) its answer text, which
    # stream() doesn't yield. Read by the status line, so a model that thinks
    # for a long time visibly does so. Reset by each stream() call.
    reasoning: str = ""

    @abstractmethod
    def stream(self, system: str, prompt: str) -> Iterator[str]:
        """Yield response text chunks as they stream in."""
        ...


def output_limit_error(name: str, limit: int, reasoned: bool) -> RuntimeError:
    """The model stopped at its output cap without answering — usually a
    reasoning model that thought too long. Say so rather than answer nothing."""
    spent = "reasoning, before answering" if reasoned else "before answering"
    return RuntimeError(
        f"{name}: the model used its whole output limit ({limit:,} tokens) {spent}. "
        "Raise `max_tokens` for this provider in your seer config, or lower its `reasoning_effort`."
    )
