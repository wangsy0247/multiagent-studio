"""Tests for YAML config system with mtime hot-reload."""
from __future__ import annotations

import asyncio
import os
import time
import threading
from pathlib import Path

import pytest

from harness.config.config_manager import ConfigManager, _interpolate_env, _interpolate_env_recursive
from harness.config.yaml_config import (
    load_yaml_config,
    resolve_env_vars,
    deep_get,
    deep_set,
    merge_configs,
    validate_config_version,
    ConfigSection,
    ModelConfig,
    SandboxConfig,
    MemoryConfig,
    SummarizationConfig,
)


# ============================================================================
# Env var resolution
# ============================================================================


class TestEnvVarResolution:
    def test_dollar_var_resolved(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        assert _interpolate_env("$MY_VAR") == "hello"
        assert _interpolate_env("${MY_VAR}") == "hello"

    def test_unset_var_returns_empty(self):
        assert _interpolate_env("$NONEXISTENT_VAR_12345") == ""

    def test_recursive_resolve_in_dict(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "sk-123")
        data = {
            "model": {"api_key": "$API_KEY"},
            "servers": [{"url": "https://$API_KEY.example.com"}],
        }
        result = _interpolate_env_recursive(data)
        assert result["model"]["api_key"] == "sk-123"
        assert result["servers"][0]["url"] == "https://sk-123.example.com"

    def test_resolve_env_vars_nested(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/test")
        result = resolve_env_vars({"path": "$HOME/data", "nested": {"key": "${HOME}/config"}})
        assert result["path"] == "/home/test/data"
        assert result["nested"]["key"] == "/home/test/config"


# ============================================================================
# deep_get / deep_set / merge
# ============================================================================


class TestDeepAccess:
    def test_deep_get_existing(self):
        data = {"a": {"b": {"c": 42}}}
        assert deep_get(data, "a.b.c") == 42

    def test_deep_get_missing_key(self):
        assert deep_get({}, "a.b.c", "fallback") == "fallback"

    def test_deep_get_non_dict(self):
        assert deep_get({"a": 1}, "a.b", None) is None

    def test_deep_set_creates_intermediate(self):
        d: dict = {}
        deep_set(d, "x.y.z", 99)
        assert d["x"]["y"]["z"] == 99

    def test_deep_set_overwrites(self):
        d = {"a": {"b": 1}}
        deep_set(d, "a.b", 2)
        assert d["a"]["b"] == 2

    def test_merge_configs(self):
        base = {"a": 1, "b": {"x": 10}}
        override = {"b": {"y": 20}, "c": 3}
        merged = merge_configs(base, override)
        assert merged["a"] == 1
        assert merged["b"] == {"x": 10, "y": 20}
        assert merged["c"] == 3

    def test_validate_config_version_warns(self):
        with pytest.warns(UserWarning):
            validate_config_version({}, expected=10)


# ============================================================================
# YAML loading
# ============================================================================


class TestLoadYaml:
    def test_load_missing_file(self):
        data = load_yaml_config("/nonexistent/path/config.yaml")
        assert data == {}

    def test_load_valid_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "test_value")
        f = tmp_path / "config.yaml"
        f.write_text("key: $TEST_KEY\nnested:\n  sub: ${TEST_KEY}_suffix")
        data = load_yaml_config(str(f))
        assert data["key"] == "test_value"
        assert data["nested"]["sub"] == "test_value_suffix"

    def test_load_empty_yaml(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        data = load_yaml_config(str(f))
        assert data == {}


# ============================================================================
# ConfigManager — hot-reload
# ============================================================================


class TestConfigManager:
    @pytest.fixture
    def cfg_file(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("config_version: 1\ndebug: false")
        return str(p)

    def test_load(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        data = mgr.load()
        assert data["config_version"] == 1
        assert data["debug"] is False

    def test_get_dot_notation(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        assert mgr.get("config_version") == 1
        assert mgr.get("debug") is False

    def test_get_missing_default(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        assert mgr.get("nonexistent.key", 42) == 42

    def test_dict_access(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        assert mgr["config_version"] == 1
        assert "config_version" in mgr

    def test_reload_if_changed(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        # No change — should return False
        assert mgr.reload_if_changed() is False
        # Modify the file
        time.sleep(0.01)  # ensure mtime advances
        with open(cfg_file, "w") as f:
            f.write("config_version: 2\ndebug: true\nnew_key: added")
        assert mgr.reload_if_changed() is True
        assert mgr.get("config_version") == 2

    def test_on_change_callback(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        results = []

        def _cb():
            results.append(mgr.get("config_version"))

        mgr.on_change(_cb)
        time.sleep(0.01)
        with open(cfg_file, "w") as f:
            f.write("config_version: 3\ndebug: true")
        mgr.reload_if_changed()
        assert len(results) == 1
        assert results[0] == 3

    def test_config_version_property(self, cfg_file):
        mgr = ConfigManager(cfg_file)
        mgr.load()
        assert mgr.config_version == 1

    def test_thread_safety(self, cfg_file):
        """Multiple threads reading config should not deadlock."""
        mgr = ConfigManager(cfg_file)
        mgr.load()
        errors = []

        def reader():
            try:
                for _ in range(50):
                    _ = mgr.get("version")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_start_stop_watcher(self, cfg_file):
        mgr = ConfigManager(cfg_file, reload_interval=0.1)
        mgr.load()
        mgr.start_watcher()
        await asyncio.sleep(0.05)
        mgr.stop_watcher()
        # Should not crash
        assert mgr._watcher_task is None or mgr._watcher_task.done()


# ============================================================================
# ConfigSection subclasses
# ============================================================================


class TestConfigSections:
    def test_model_config_from_dict(self):
        data = {
            "name": "gpt-4o",
            "display_name": "GPT-4o",
            "model": "gpt-4o",
            "api_key": "$OPENAI_API_KEY",
            "temperature": 0.7,
            "max_tokens": 4096,
            "supports_vision": True,
        }
        mc = ModelConfig.from_dict(data)
        assert mc.name == "gpt-4o"
        assert mc.temperature == 0.7
        assert mc.supports_vision is True

    def test_sandbox_config_defaults(self):
        sc = SandboxConfig.from_dict({})
        assert sc.allow_host_bash is False
        assert sc.bash_output_max_chars == 10000

    def test_memory_config(self):
        mc = MemoryConfig.from_dict({
            "enabled": True,
            "storage_path": "/data/memory.json",
            "max_facts": 200,
        })
        assert mc.enabled is True
        assert mc.max_facts == 200

    def test_summarization_config(self):
        sc = SummarizationConfig.from_dict({
            "enabled": True,
            "trigger": [{"type": "tokens", "value": 32000}],
            "keep": {"type": "messages", "value": 10},
        })
        assert sc.enabled is True
        assert len(sc.trigger) == 1
        assert sc.trigger[0]["type"] == "tokens"
