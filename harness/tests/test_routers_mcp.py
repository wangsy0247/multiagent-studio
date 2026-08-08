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
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), cfg


def _read(cfg):
    return json.loads(cfg.read_text())


class TestMcpServersApi:
    def test_list(self, client):
        c, cfg = client
        resp = c.get("/api/mcp/servers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["servers"]["github"]["type"] == "stdio"

    def test_upsert_create_stdio(self, client):
        c, cfg = client
        resp = c.put("/api/mcp/servers/brave", json={
            "type": "stdio", "command": "npx", "args": ["-y", "brave-search"],
            "env": {"KEY": "$BRAVE_KEY"}, "description": "搜索",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "created"
        entry = _read(cfg)["mcpServers"]["brave"]
        assert entry["enabled"] is True
        assert entry["command"] == "npx"

    def test_upsert_http_requires_url(self, client):
        c, _ = client
        resp = c.put("/api/mcp/servers/remote", json={"type": "http"})
        assert resp.status_code == 400

    def test_upsert_stdio_requires_command(self, client):
        c, _ = client
        resp = c.put("/api/mcp/servers/bad", json={"type": "stdio"})
        assert resp.status_code == 400

    def test_invalid_name_rejected(self, client):
        c, _ = client
        resp = c.put("/api/mcp/servers/bad%20name", json={
            "type": "stdio", "command": "npx",
        })
        assert resp.status_code == 400

    def test_enabled_toggle(self, client):
        c, cfg = client
        resp = c.put("/api/mcp/servers/github/enabled", json={"enabled": False})
        assert resp.status_code == 200
        assert _read(cfg)["mcpServers"]["github"]["enabled"] is False

    def test_enabled_toggle_404(self, client):
        c, _ = client
        resp = c.put("/api/mcp/servers/ghost/enabled", json={"enabled": False})
        assert resp.status_code == 404

    def test_delete(self, client):
        c, cfg = client
        resp = c.delete("/api/mcp/servers/github")
        assert resp.status_code == 200
        assert "github" not in _read(cfg)["mcpServers"]

    def test_delete_404(self, client):
        c, _ = client
        assert c.delete("/api/mcp/servers/ghost").status_code == 404
