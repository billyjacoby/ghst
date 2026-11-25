"""Tests for the provider/model setup flow, profiles and the picker."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from ghst import picker, setup
from ghst.config import GhstConfig, ProviderConfig, profile_id


class TestPicker:
    options = [picker.Option("gpt-5-nano", "fast"), picker.Option("gpt-5"), picker.Option("o4")]

    def test_filter_matches_every_word(self) -> None:
        assert picker.filter_options(self.options, "gpt") == [0, 1]
        assert picker.filter_options(self.options, "gpt fast") == [0]
        assert picker.filter_options(self.options, "zzz") == []

    @pytest.mark.parametrize(("answer", "expected"), [
        ("", 1), ("3", 2), ("nano", 0), ("nothing", None),
    ])
    def test_numbered_fallback(
        self, monkeypatch: pytest.MonkeyPatch, answer: str, expected: int | None,
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda _prompt: answer)
        assert picker.pick("Model:", self.options, default=1) == expected

    def test_numbered_fallback_without_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert picker.pick("Model:", self.options, default=2) == 2


class TestProfiles:
    @pytest.mark.parametrize(("provider", "expected"), [
        (ProviderConfig(name="openai", api_base_url="http://localhost:8000/v1"), "local"),
        (ProviderConfig(name="openai"), "openai"),
        (ProviderConfig(name="openai", api_base_url="https://api.openai.com/v1/"), "openai"),
        (ProviderConfig(name="anthropic"), "anthropic"),
        (ProviderConfig(name="chatgpt"), "chatgpt"),
    ])
    def test_profile_id(self, provider: ProviderConfig, expected: str) -> None:
        assert profile_id(provider) == expected

    def test_round_trip_and_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        config = GhstConfig(config_path=path)
        config.provider = ProviderConfig(
            name="openai", api_key="k-local", api_base_url="http://localhost:8000/v1", model="q",
        )
        config.save_profile()
        config.provider = ProviderConfig(name="anthropic", api_key="k-ant", model="claude")
        config.save_profile()
        config.write_toml()

        loaded = GhstConfig.load(path)
        assert loaded.provider.name == "anthropic"
        assert loaded.profiles["local"].api_key == "k-local"
        assert loaded.profiles["local"].api_base_url == "http://localhost:8000/v1"
        assert loaded.profiles["anthropic"].model == "claude"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_env_key_is_not_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GHST_API_KEY", "from-env")
        path = tmp_path / "config.toml"
        config = GhstConfig.load(path)
        assert config.provider.api_key == "from-env"
        config.write_toml()
        assert "from-env" not in path.read_text()


class TestModelChoices:
    def test_openai_list_keeps_chat_models(self) -> None:
        models = ["whisper-1", "gpt-5-nano", "text-embedding-3", "gpt-4o-audio", "o4-mini", "gpt-5"]
        assert setup.usable_models("openai", models) == ["gpt-5", "gpt-5-nano", "o4-mini"]

    def test_other_providers_keep_everything(self) -> None:
        assert setup.usable_models("local", ["b", "a"]) == ["b", "a"]

    def test_default_prefers_current_then_fast(self) -> None:
        models = ["big-model", "Qwen2.5-Coder-1.5B", "other"]
        assert setup._default_index(models, "other", prefer_fast=True) == 2
        assert setup._default_index(models, "missing", prefer_fast=True) == 1
        assert setup._default_index(models, "missing", prefer_fast=False) == 0


class TestSetupFlow:
    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Scripted answers, a fake server and no real ghost-text request."""
        state: dict[str, Any] = {"answers": [], "probed": []}
        omlx = tmp_path / "omlx.json"
        omlx.write_text(json.dumps({"auth": {"api_key": "omlx-key"}}))
        monkeypatch.setattr(setup, "OMLX_SETTINGS", omlx)
        monkeypatch.delenv("GHST_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", lambda _p: state["answers"].pop(0))
        monkeypatch.setattr(setup.getpass, "getpass", lambda _p: state["answers"].pop(0))

        def fake_pick(title: str, options: list[picker.Option], default: int = 0) -> int | None:
            return default  # Enter on the preselected option

        def fake_probe(config: GhstConfig) -> dict[str, Any]:
            state["probed"].append((config.provider.api_base_url, config.provider.api_key))
            return {"ok": True, "url": "x", "models": ["big-30b", "coder-1.5b"]}

        monkeypatch.setattr(setup, "pick", fake_pick)
        monkeypatch.setattr(setup, "probe", fake_probe)
        monkeypatch.setattr(setup, "try_ghost_text", lambda config: ("sh", 12.0, ""))
        state["path"] = tmp_path / "config.toml"
        return state

    def test_connect_local_server_with_omlx_key(self, env: dict[str, Any]) -> None:
        config = GhstConfig.load(env["path"])
        env["answers"] = ["", ""]  # keep the default URL, accept oMLX's key
        assert setup.setup_provider(config, "local")
        assert env["probed"] == [("http://localhost:8000/v1", "omlx-key")]
        saved = GhstConfig.load(env["path"])
        assert saved.provider.autocomplete_model == "coder-1.5b"
        assert saved.provider.model == "big-30b"
        assert saved.profiles["local"].api_key == "omlx-key"

    def test_switching_back_restores_saved_settings(self, env: dict[str, Any]) -> None:
        config = GhstConfig.load(env["path"])
        env["answers"] = ["", ""]
        assert setup.setup_provider(config, "local")
        env["answers"] = ["ant-key"]
        assert setup.setup_provider(config, "anthropic")
        assert GhstConfig.load(env["path"]).provider.name == "anthropic"

        env["answers"] = ["", ""]  # Enter keeps the saved URL and the saved key
        assert setup.setup_provider(config, "local")
        saved = GhstConfig.load(env["path"])
        assert saved.provider.api_base_url == "http://localhost:8000/v1"
        assert saved.provider.api_key == "omlx-key"
        assert saved.profiles["anthropic"].api_key == "ant-key"

    def test_cancelled_model_pick_changes_nothing(self, env: dict[str, Any]) -> None:
        config = GhstConfig.load(env["path"])
        env["answers"] = ["", ""]

        def cancel(*_args: Any, **_kwargs: Any) -> None:
            return None

        setup.pick = cancel  # type: ignore[assignment]
        assert not setup.setup_provider(config, "local")
        assert not env["path"].exists()

    def test_api_provider_needs_a_key(self, env: dict[str, Any]) -> None:
        config = GhstConfig.load(env["path"])
        env["answers"] = [""]
        assert not setup.setup_provider(config, "openai")
