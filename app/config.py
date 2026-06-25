"""App 服务统一配置 — Pydantic Settings 管理所有环境变量。"""

import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_WEAK_JWT_SECRETS = {
    "change-me-in-production",
    "change-me-in-production-use-random-string",
    "",
    "secret",
    "changeme",
}


class AppSettings(BaseSettings):
    """App 服务配置，所有字段有校验。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── JWT ──────────────────────────────────────────────
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24h

    # ── Database ─────────────────────────────────────────
    database_url: str = ""

    # ── CORS ─────────────────────────────────────────────
    cors_origins: str = "http://localhost:3000"

    # ── Harness ──────────────────────────────────────────
    harness_url: str = "http://localhost:8001"

    # ── Workspace ────────────────────────────────────────
    workspace_root: str = "~/.multiagent-studio/workspace"

    # ── Harness Data Root ────────────────────────────────
    # Uploads are stored in the Harness data layout so the sandbox provider
    # and UploadsMiddleware can access them directly.
    harness_data_root: str = "~/.multiagent-studio"

    def validate_jwt_secret(self) -> None:
        """启动时校验 JWT_SECRET 不是弱密钥。若未设置则自动生成。"""
        if not self.jwt_secret or self.jwt_secret.lower() in _WEAK_JWT_SECRETS:
            # 尝试从原始环境变量读取（pydantic-settings 已做了 alias 映射）
            raw = os.getenv("JWT_SECRET", "")
            if raw and raw.lower() not in _WEAK_JWT_SECRETS:
                self.jwt_secret = raw
                return

            # 未设置或为弱密钥 → 生成强随机密钥
            generated = secrets.token_urlsafe(32)
            self.jwt_secret = generated
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "⚠ JWT_SECRET 未设置或为弱密钥，已自动生成强随机密钥。"
                "请在 .env 中设置 JWT_SECRET 以保持重启后 Token 有效。"
            )

    def get_cors_origins(self) -> list[str]:
        """返回解析后的 CORS origins 列表。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


_settings: AppSettings | None = None


def get_settings() -> AppSettings:
    """获取全局配置单例（懒加载 + 校验）。"""
    global _settings
    if _settings is None:
        _settings = AppSettings()  # type: ignore[call-arg]
        _settings.workspace_root = os.path.expanduser(_settings.workspace_root)
        _settings.harness_data_root = os.path.expanduser(_settings.harness_data_root)
        _settings.validate_jwt_secret()
    return _settings
