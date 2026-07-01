"""Tests for ConfigManager with mtime hot-reload."""
from __future__ import annotations

import asyncio
import time
import threading

import pytest

from harness.config.config_manager import ConfigManager, _interpolate_env, _interpolate_env_recursive


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
        assert mgr.reload_if_changed() is False
        time.sleep(0.01)
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
        assert mgr._watcher_task is None or mgr._watcher_task.done()
