"""agent-skills 聚合端点: 按 agent 列出成员私有进化技能."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.api.routers_skills import router


@pytest.fixture
def client(tmp_path):
    from harness.config.paths import Paths, set_paths, get_paths

    old = get_paths()
    set_paths(Paths(base_dir=tmp_path))
    try:
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app), tmp_path
    finally:
        set_paths(old)


def _mk_agent(uid: str, name: str) -> None:
    from harness.config.agents_config import AgentConfig, save_agent_config

    save_agent_config(name, AgentConfig(name=name), user_id=uid)


def _mk_agent_skill(uid: str, agent: str, skill_name: str) -> None:
    from harness.skills.evolution.member import MemberSkillEvolutionStore

    store = MemberSkillEvolutionStore(user_id=uid)
    store.add_candidate(
        agent,
        f"---\nname: {skill_name}\ndescription: 测试技能\n---\n\n内容\n",
    )


class TestAgentSkillsApi:
    def test_aggregates_per_agent(self, client):
        c, _ = client
        _mk_agent("u1", "coder")
        _mk_agent("u1", "writer")
        _mk_agent_skill("u1", "coder", "pytest-tips")

        resp = c.get("/api/skills/agent-skills?user_id=u1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1  # writer 无技能, 不返回
        entry = data["agents"][0]
        assert entry["agent"] == "coder"
        skill = entry["skills"][0]
        assert skill["name"] == "pytest-tips"
        assert skill["state"] == "probation"
        assert skill["success_uses"] == 0

    def test_empty_when_no_agent_skills(self, client):
        c, _ = client
        _mk_agent("u1", "coder")
        resp = c.get("/api/skills/agent-skills?user_id=u1")
        assert resp.status_code == 200
        assert resp.json() == {"agents": [], "count": 0}

    def test_not_swallowed_by_name_route(self, client):
        """/agent-skills 不能被 /{name} 路径参数匹配掉."""
        c, _ = client
        resp = c.get("/api/skills/agent-skills")
        # 命中聚合端点 (200) 而非 get_skill("agent-skills") (404/400)
        assert resp.status_code == 200
