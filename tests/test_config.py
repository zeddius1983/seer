"""Tests for provider/model auto-resolution in config.py."""

import pytest
from unittest.mock import patch, call

from seer.config import Config, ProviderConfig, _list_openai_models


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(provider: str, providers: dict) -> Config:
    return Config(provider=provider, providers=providers)


LMSTUDIO = {
    "type": "openai",
    "base_url": "http://127.0.0.1:1234/v1",
    "api_key": "lmstudio",
    "model": "auto",
}

OLLAMA = {
    "type": "openai",
    "base_url": "http://localhost:11434/v1",
    "api_key": "ollama",
    "model": "auto",
}

LLAMACPP = {
    "type": "openai",
    "base_url": "http://127.0.0.1:8080/v1",
    "api_key": "llamacpp",
    "model": "auto",
}

OPENAI_CLOUD = {
    "type": "openai",
    "model": "gpt-4o",
}

ANTHROPIC_CLOUD = {
    "type": "anthropic",
    "model": "claude-sonnet-4-6",
}


# ---------------------------------------------------------------------------
# _list_openai_models
# ---------------------------------------------------------------------------

class TestListOpenaiModels:
    def test_returns_model_ids_on_success(self):
        mock_resp = {"data": [{"id": "gemma-3"}, {"id": "llama3"}]}
        with patch("seer.config.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_resp

            result = _list_openai_models("http://127.0.0.1:1234/v1", "key")

        assert result == ["gemma-3", "llama3"]
        mock_get.assert_called_once_with(
            "http://127.0.0.1:1234/v1/models",
            headers={"Authorization": "Bearer key"},
            timeout=2.0,
        )

    def test_returns_empty_on_non_200(self):
        with patch("seer.config.httpx.get") as mock_get:
            mock_get.return_value.status_code = 503
            assert _list_openai_models("http://127.0.0.1:1234/v1", "key") == []

    def test_returns_empty_on_connection_error(self):
        with patch("seer.config.httpx.get", side_effect=Exception("connection refused")):
            assert _list_openai_models("http://127.0.0.1:1234/v1", "key") == []

    def test_strips_trailing_slash_from_base_url(self):
        with patch("seer.config.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"data": [{"id": "m1"}]}

            _list_openai_models("http://127.0.0.1:1234/v1/", "key")

        args, _ = mock_get.call_args
        assert args[0] == "http://127.0.0.1:1234/v1/models"

    def test_skips_entries_without_id(self):
        mock_resp = {"data": [{"id": "good-model"}, {"name": "no-id-field"}]}
        with patch("seer.config.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_resp

            result = _list_openai_models("http://127.0.0.1:1234/v1", "key")

        assert result == ["good-model"]


# ---------------------------------------------------------------------------
# provider: auto
# ---------------------------------------------------------------------------

class TestAutoProvider:
    def test_selects_first_reachable_provider(self):
        cfg = _make_config("auto", {"llamacpp": LLAMACPP, "lmstudio": LMSTUDIO, "ollama": OLLAMA})

        def probe(base_url, api_key):
            if "8080" in base_url:
                return []  # llamacpp down
            if "1234" in base_url:
                return ["gemma-3"]  # lmstudio up
            return ["llama3"]

        with patch("seer.config._list_openai_models", side_effect=probe):
            pcfg = cfg.get_active_provider()

        assert pcfg.name == "lmstudio"

    def test_skips_providers_without_base_url(self):
        cfg = _make_config("auto", {
            "openai": OPENAI_CLOUD,     # no base_url
            "lmstudio": LMSTUDIO,
        })
        with patch("seer.config._list_openai_models", return_value=["gemma-3"]) as mock_probe:
            pcfg = cfg.get_active_provider()

        assert pcfg.name == "lmstudio"
        # openai (no base_url) must never be probed
        for c in mock_probe.call_args_list:
            assert "1234" in c.args[0]

    def test_skips_anthropic_type_providers(self):
        cfg = _make_config("auto", {
            "anthropic": ANTHROPIC_CLOUD,
            "lmstudio": LMSTUDIO,
        })
        with patch("seer.config._list_openai_models", return_value=["gemma-3"]) as mock_probe:
            cfg.get_active_provider()

        for c in mock_probe.call_args_list:
            assert "1234" in c.args[0]

    def test_raises_when_no_provider_reachable(self):
        cfg = _make_config("auto", {"llamacpp": LLAMACPP, "lmstudio": LMSTUDIO})
        with patch("seer.config._list_openai_models", return_value=[]):
            with pytest.raises(ValueError, match="no configured provider is reachable"):
                cfg.get_active_provider()

    def test_resolved_name_set_on_provider_config(self):
        cfg = _make_config("auto", {"lmstudio": LMSTUDIO})
        with patch("seer.config._list_openai_models", return_value=["gemma-3"]):
            pcfg = cfg.get_active_provider()
        assert pcfg.name == "lmstudio"


# ---------------------------------------------------------------------------
# model: auto
# ---------------------------------------------------------------------------

class TestAutoModel:
    def test_resolves_to_first_model_in_list(self):
        cfg = _make_config("lmstudio", {"lmstudio": LMSTUDIO})
        with patch("seer.config._list_openai_models", return_value=["gemma-3", "llama3"]):
            pcfg = cfg.get_active_provider()
        assert pcfg.model == "gemma-3"
        assert pcfg.model_was_auto is True

    def test_raises_when_model_list_empty(self):
        cfg = _make_config("lmstudio", {"lmstudio": LMSTUDIO})
        with patch("seer.config._list_openai_models", return_value=[]):
            with pytest.raises(ValueError, match="model: auto"):
                cfg.get_active_provider()

    def test_explicit_model_bypasses_probe(self):
        explicit = {**LMSTUDIO, "model": "my-model"}
        cfg = _make_config("lmstudio", {"lmstudio": explicit})
        with patch("seer.config._list_openai_models") as mock_probe:
            pcfg = cfg.get_active_provider()
        mock_probe.assert_not_called()
        assert pcfg.model == "my-model"
        assert pcfg.model_was_auto is False


# ---------------------------------------------------------------------------
# provider: auto + model: auto — single probe, no duplicate HTTP call
# ---------------------------------------------------------------------------

class TestAutoProviderAndModel:
    def test_single_http_probe_for_both(self):
        cfg = _make_config("auto", {"lmstudio": LMSTUDIO})
        with patch("seer.config._list_openai_models", return_value=["gemma-3"]) as mock_probe:
            pcfg = cfg.get_active_provider()

        assert mock_probe.call_count == 1
        assert pcfg.name == "lmstudio"
        assert pcfg.model == "gemma-3"
        assert pcfg.model_was_auto is True

    def test_provider_order_respected(self):
        cfg = _make_config("auto", {
            "llamacpp": LLAMACPP,
            "lmstudio": LMSTUDIO,
            "ollama": OLLAMA,
        })

        def probe(base_url, api_key):
            return ["llama3"] if "11434" in base_url else []

        with patch("seer.config._list_openai_models", side_effect=probe):
            pcfg = cfg.get_active_provider()

        assert pcfg.name == "ollama"
        assert pcfg.model == "llama3"


# ---------------------------------------------------------------------------
# Explicit provider (no auto) — sanity checks
# ---------------------------------------------------------------------------

class TestExplicitProvider:
    def test_explicit_provider_explicit_model(self):
        raw = {**LMSTUDIO, "model": "gemma-3"}
        cfg = _make_config("lmstudio", {"lmstudio": raw})
        with patch("seer.config._list_openai_models") as mock_probe:
            pcfg = cfg.get_active_provider()
        mock_probe.assert_not_called()
        assert pcfg.model == "gemma-3"
        assert pcfg.name == "lmstudio"

    def test_unknown_provider_raises(self):
        cfg = _make_config("missing", {"lmstudio": LMSTUDIO})
        with pytest.raises(ValueError, match="not found in config"):
            cfg.get_active_provider()
