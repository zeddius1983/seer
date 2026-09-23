from .base import Provider
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from .cli import ClaudeCLIProvider, CodexCLIProvider
from ..config import ProviderConfig


def get_provider(cfg: ProviderConfig) -> Provider:
    if cfg.type == "anthropic":
        return AnthropicProvider(cfg)
    elif cfg.type == "openai":
        return OpenAIProvider(cfg)
    elif cfg.type == "claude-cli":
        return ClaudeCLIProvider(cfg)
    elif cfg.type == "codex-cli":
        return CodexCLIProvider(cfg)
    else:
        raise ValueError(
            f"Unknown provider type: '{cfg.type}'. "
            "Use 'openai', 'anthropic', 'claude-cli' or 'codex-cli'."
        )
