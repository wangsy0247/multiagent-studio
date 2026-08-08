"""扩展配置写操作的审计日志.

所有经 REST API 对全局扩展配置 (MCP server 等) 的写操作都会追加一条
JSONL 记录到 ``{data_root}/audit/extensions.jsonl`` — 公司内部部署时
用于追溯"谁在什么时候改了什么"。

脱敏规则: env / headers 等可能携带凭证的字段只记录键名, 值替换为 "***"。
审计写入永不抛出 — 失败仅记 warning, 不阻断业务操作。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 值需要脱敏的字段名
_REDACTED_KEYS = ("env", "headers")


def _redact(detail: Any) -> Any:
    """递归脱敏: env/headers 字典的值替换为 "***"."""
    if isinstance(detail, dict):
        out: dict[str, Any] = {}
        for k, v in detail.items():
            if k in _REDACTED_KEYS and isinstance(v, dict):
                out[k] = {kk: "***" for kk in v}
            else:
                out[k] = _redact(v)
        return out
    if isinstance(detail, list):
        return [_redact(item) for item in detail]
    return detail


def log_audit(
    action: str,
    *,
    user_id: str = "unknown",
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """追加一条审计记录. 永不抛出.

    Args:
        action: 操作标识, 如 "mcp.upsert" / "mcp.enable" / "mcp.delete".
        user_id: 操作者 (app 代理经 query param 透传的 JWT 用户名).
        target: 操作对象 (如 MCP server 名).
        detail: 附加信息 (自动脱敏).
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": user_id,
        "action": action,
        "target": target,
        "detail": _redact(detail or {}),
    }
    try:
        from harness.config.paths import get_paths

        audit_dir = get_paths().base_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / "extensions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("Audit log write failed: %s", record, exc_info=True)
    logger.info(
        "audit: %s %s by user=%s", action, target, user_id,
    )
