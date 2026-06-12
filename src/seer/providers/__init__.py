from .base import Provider
from .openai import OpenAIProvider
from .anthropic import AnthropicProvider
from ..config import ProviderConfig


def get_provider(cfg: ProviderConfig) -> Provider:
    if cfg.type == "anthropic":
        return AnthropicProvider(cfg)
    elif cfg.type == "openai":
        return OpenAIProvider(cfg)
    else:
        raise ValueError(f"Unknown provider type: '{cfg.type}'. Use 'openai' or 'anthropic'.")
