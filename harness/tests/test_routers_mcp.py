"""MCP server 管理 API (routers_mcp) — CRUD + 校验 + 缓存失效."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.api.routers_mcp import router


@pytest.fixture
def client(monkeypatch, tmp_path):
    cfg = tmp_path / "extensions_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "github": {"enabled": True, "type": "stdio", "command": "npx",
                        "args": ["-y", "gh-server"], "env": {}, "description": "gh"},
        },
        "skills": {},
    }))
    monkeypatch.setenv("EXTENSIONS_CONFIG_PATH", str(cfg))
    # 审计日志写入 {base_dir}/audit/ — 隔离到 tmp_path
    from harness.config.paths import Paths, get_paths, set_paths
    old_paths = get_paths()
    set_paths(Paths(str(tmp_path)))
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), cfg, tmp_path
    set_paths(old_paths)


def _read(cfg):
    return json.loads(cfg.read_text())


def _read_audit(tmp_path):
    audit_file = tmp_path / "audit" / "extensions.jsonl"
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text().splitlines()]


class TestMcpServersApi:
    def test_list(self, client):
        c, cfg, _ = client
        resp = c.get("/api/mcp/servers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["servers"]["github"]["type"] == "stdio"

    def test_upsert_stdio_rejected(self, client):
        """stdio 等价于服务器上执行任意命令 — API 一律拒绝, 管理员走配置文件."""
        c, cfg, _ = client
        resp = c.put("/api/mcp/servers/brave", json={
            "type": "stdio", "command": "npx", "args": ["-y", "brave-search"],
            "env": {"KEY": "$BRAVE_KEY"}, "description": "搜索",
        })
        assert resp.status_code == 400
        assert "stdio" in resp.json()["detail"]
        assert "brave" not in _read(cfg)["mcpServers"]

    def test_upsert_create_http(self, client):
        c, cfg, tmp_path = client
        resp = c.put("/api/mcp/servers/remote?user_id=alice", json={
            "type": "http", "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer secret-token"}, "description": "远程",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
        entry = _read(cfg)["mcpServers"]["remote"]
        assert entry["url"] == "https://mcp.example.com/sse"

        # 审计: 记录操作者与动作, headers 值脱敏
        audit = _read_audit(tmp_path)
        assert len(audit) == 1
        rec = audit[0]
        assert rec["action"] == "mcp.create"
        assert rec["user"] == "alice"
        assert rec["target"] == "remote"
        assert rec["detail"]["headers"] == {"Authorization": "***"}

    def test_upsert_http_requires_url(self, client):
        c, _, _ = client
        resp = c.put("/api/mcp/servers/remote", json={"type": "http"})
        assert resp.status_code == 400

    def test_invalid_name_rejected(self, client):
        c, _, _ = client
        resp = c.put("/api/mcp/servers/bad%20name", json={
            "type": "http", "url": "https://x.example.com",
        })
        assert resp.status_code == 400

    def test_enabled_toggle(self, client):
        c, cfg, tmp_path = client
        resp = c.put("/api/mcp/servers/github/enabled?user_id=bob", json={"enabled": False})
        assert resp.status_code == 200
        assert _read(cfg)["mcpServers"]["github"]["enabled"] is False
        audit = _read_audit(tmp_path)
        assert audit[-1]["action"] == "mcp.disable"
        assert audit[-1]["user"] == "bob"

    def test_enabled_toggle_404(self, client):
        c, _, _ = client
        resp = c.put("/api/mcp/servers/ghost/enabled", json={"enabled": False})
        assert resp.status_code == 404

    def test_delete(self, client):
        c, cfg, tmp_path = client
        resp = c.delete("/api/mcp/servers/github?user_id=carol")
        assert resp.status_code == 200
        assert "github" not in _read(cfg)["mcpServers"]
        audit = _read_audit(tmp_path)
        assert audit[-1]["action"] == "mcp.delete"
        assert audit[-1]["user"] == "carol"

    def test_delete_404(self, client):
        c, _, _ = client
        assert c.delete("/api/mcp/servers/ghost").status_code == 404
