"""服务器统一模型配置: env 注入 + SERVER_FORCED_KEYS 强制覆盖 + L1 格式化收窄."""
from __future__ import annotations

import yaml
import pytest

from harness.config.config_loader import (
    ConfigLoader,
    SERVER_FORCED_KEYS,
    create_user_configs,
    format_user_global_config_yaml,
)

_MODEL_ENV_VARS = (
    "DEFAULT_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "SUMMARY_MODEL",
    "TITLE_MODEL",
    "MEMORY_MODEL",
)


@pytest.fixture
def clean_model_env(monkeypatch):
    """清空模型相关环境变量, 避免 harness/.env 的真实值泄漏进测试."""
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def user_dir(tmp_path):
    """构造带陈旧模型字段的用户目录 (L1 + L2)."""
    ud = tmp_path / "users" / "u1"
    (ud / "agents" / "coder").mkdir(parents=True)
    (ud / "config.yaml").write_text(yaml.safe_dump({
        "api_key": "sk-stale",
        "base_url": "http://stale",
        "default_model": "m-stale",
        "memory": {"max_facts": 42},
    }))
    (ud / "agents" / "coder" / "config.yaml").write_text(yaml.safe_dump({
        "name": "coder",
        "model": "m-agent-stale",
        "temperature": 0.7,
    }))
    return tmp_path


class TestServerForcedModelKeys:
    def test_env_injection_overrides_stale_user_yaml(self, clean_model_env, user_dir):
        clean_model_env.setenv("OPENAI_API_KEY", "sk-server")
        clean_model_env.setenv("DEFAULT_MODEL", "m-server")
        clean_model_env.setenv("OPENAI_BASE_URL", "http://server")

        eff = ConfigLoader.load_effective("u1", "coder", base_dir=user_dir)
        assert eff.model == "m-server"
        assert eff.api_key == "sk-server"
        assert eff.base_url == "http://server"
        # 非服务器字段仍按层级合并生效
        assert eff.temperature == 0.7
        assert eff.memory_max_facts == 42

    def test_aux_models_fallback_empty_when_env_unset(self, clean_model_env, user_dir):
        eff = ConfigLoader.load_effective("u1", "coder", base_dir=user_dir)
        assert eff.summary_model == ""
        assert eff.title_model == ""
        assert eff.memory_model == ""

    def test_defaults_when_env_unset(self, clean_model_env, user_dir):
        """未配置 env 时回退到默认值 — 用户 YAML 的 model/api_key 依然无效."""
        eff = ConfigLoader.load_effective("u1", "coder", base_dir=user_dir)
        assert eff.model == "gpt-4o"
        assert eff.api_key == ""
        assert eff.base_url == "https://api.openai.com/v1"

    def test_all_forced_keys_covered_by_l0(self):
        from harness.config.defaults import SYSTEM_DEFAULTS
        for key in SERVER_FORCED_KEYS:
            assert key in SYSTEM_DEFAULTS, f"{key} 必须在 SYSTEM_DEFAULTS 中有默认值"


class TestUserGlobalConfigFormat:
    def test_format_excludes_model_fields(self):
        out = format_user_global_config_yaml({"memory": {"max_facts": 50, "ttl_days": 30}})
        for field in ("api_key:", "base_url:", "default_model:", "summary_model:",
                      "title_model:", "memory_model:"):
            assert field not in out
        assert "max_facts: 50" in out
        assert "ttl_days: 30" in out

    def test_format_preserves_extra_infra_fields(self):
        extra = {"sandbox": {"image": "python:3.12"}, "checkpointer": {"backend": "sqlite"}}
        out = format_user_global_config_yaml({}, extra=extra)
        parsed = yaml.safe_load(out)
        assert parsed["sandbox"]["image"] == "python:3.12"
        assert parsed["checkpointer"]["backend"] == "sqlite"

    def test_new_user_template_has_no_api_key(self, clean_model_env, tmp_path):
        create_user_configs("newuser", base_dir=tmp_path)
        cfg = ConfigLoader.load_user_global("newuser", base_dir=tmp_path)
        assert cfg is not None
        assert "api_key" not in cfg
        assert "default_model" not in cfg
        assert "memory" in cfg


class TestInitLlmFallback:
    """_init_llm 裸调用 (无参 + contextvar 为空) 必须回退到服务器 env,
    而不是硬编码 gpt-4o + 空 key — 澄清恢复路径曾是这类裸调用的来源."""

    def test_bare_call_falls_back_to_server_env(self, clean_model_env):
        clean_model_env.setenv("DEFAULT_MODEL", "qwen3.6-plus")
        clean_model_env.setenv("OPENAI_API_KEY", "sk-server")
        clean_model_env.setenv("OPENAI_BASE_URL", "http://dashscope")

        from harness.main import HarnessService, _current_req_creds

        token = _current_req_creds.set({})  # 确保无请求上下文
        try:
            svc = HarnessService()
            llm = svc._init_llm()
        finally:
            _current_req_creds.reset(token)

        assert llm.model_name == "qwen3.6-plus"
        assert llm.openai_api_key.get_secret_value() == "sk-server"
        assert llm.openai_api_base == "http://dashscope"

    def test_bare_call_without_env_keeps_last_resort(self, clean_model_env):
        from harness.main import HarnessService, _current_req_creds

        token = _current_req_creds.set({})
        try:
            svc = HarnessService()
            llm = svc._init_llm()
        finally:
            _current_req_creds.reset(token)

        # env 全空时的最后兜底: 占位 key + gpt-4o (仅提示作用, 不会误连真模型)
        assert llm.model_name == "gpt-4o"
        assert llm.openai_api_key.get_secret_value() == "MISSING_API_KEY_CONFIGURED"

    def test_contextvar_takes_precedence_over_env(self, clean_model_env):
        clean_model_env.setenv("DEFAULT_MODEL", "qwen3.6-plus")
        clean_model_env.setenv("OPENAI_API_KEY", "sk-server")

        from harness.main import HarnessService, _current_req_creds

        token = _current_req_creds.set({
            "model": "ctx-model", "api_key": "sk-ctx", "base_url": "http://ctx",
        })
        try:
            svc = HarnessService()
            llm = svc._init_llm()
        finally:
            _current_req_creds.reset(token)

        assert llm.model_name == "ctx-model"
        assert llm.openai_api_key.get_secret_value() == "sk-ctx"
