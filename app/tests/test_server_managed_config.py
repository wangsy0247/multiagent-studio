"""服务器统一模型配置: agent 创建免 model、PUT config/global 白名单 + 字段保留."""
from __future__ import annotations

import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models.configuration  # noqa: F401
import app.models.file_record  # noqa: F401
import app.models.message  # noqa: F401
import app.models.scheduled_task  # noqa: F401
import app.models.thread  # noqa: F401
import app.models.user  # noqa: F401
from app.api import agents as agents_api
from app.models.user import User


@pytest.fixture(autouse=True)
def temp_data_root(tmp_path):
    """隔离数据根目录 — get_paths() 是进程级单例, 必须用 set_paths 切换
    (monkeypatch HARNESS_DATA_ROOT 对 HarnessConfig 无效, 会写到真实数据目录)."""
    from harness.config.paths import Paths, get_paths, set_paths

    old = get_paths()
    set_paths(Paths(base_dir=tmp_path))
    yield tmp_path
    set_paths(old)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_user(db, username: str) -> User:
    user = User(email=f"{username}@b.com", username=username, hashed_password="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class _Req:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _user_cfg_path(username: str):
    from harness.config.paths import get_paths

    return get_paths().base_dir / "users" / username / "config.yaml"


@pytest.mark.asyncio
async def test_create_agent_without_model_succeeds(db, temp_data_root):
    """模型由服务器统一配置 — 创建 agent 不再要求 model 字段."""
    alice = await _make_user(db, "alice")
    body = {"name": "writer", "display_name": "Writer", "temperature": 0.5}
    result = await agents_api.create_agent(_Req(body), db, alice)
    assert result["status"] == "created"
    assert "model" not in result

    from harness.config.agents_config import load_agent_config

    cfg = load_agent_config("writer", user_id="alice")
    assert cfg is not None
    assert cfg.model == ""  # 不写入陈旧模型值
    assert cfg.temperature == 0.5


@pytest.mark.asyncio
async def test_create_agent_ignores_model_in_body(db, temp_data_root):
    """即使 body 携带 model 也不落盘 (避免残留失效值)."""
    alice = await _make_user(db, "alice")
    body = {"name": "coder", "model": "m-stale"}
    result = await agents_api.create_agent(_Req(body), db, alice)
    assert result["status"] == "created"

    from harness.config.agents_config import load_agent_config

    cfg = load_agent_config("coder", user_id="alice")
    assert cfg is not None
    assert cfg.model == ""


@pytest.mark.asyncio
async def test_agent_extensions_roundtrip(db, temp_data_root):
    """create/update agent 写 extensions_config.yaml, GET 返回 extensions."""
    alice = await _make_user(db, "alice")
    body = {"name": "coder", "mcp_servers": {"github": False},
            "skills_enabled": {"deep-research": False}}
    result = await agents_api.create_agent(_Req(body), db, alice)
    assert result["status"] == "created"

    got = await agents_api.get_agent("coder", db, alice)
    assert got["extensions"]["mcp_servers"] == {"github": False}
    assert got["extensions"]["skills"] == {"deep-research": False}

    # update: 只改 mcp_servers, skills 保留
    upd = {"mcp_servers": {"github": False, "filesystem": False}}
    await agents_api.update_agent("coder", _Req(upd), db, alice)
    got = await agents_api.get_agent("coder", db, alice)
    assert got["extensions"]["mcp_servers"] == {"github": False, "filesystem": False}
    assert got["extensions"]["skills"] == {"deep-research": False}


@pytest.mark.asyncio
async def test_put_global_config_whitelist_and_preserve_infra(db, temp_data_root):
    """PUT config/global: 白名单外字段拒绝写入, 基础设施字段原样保留."""
    alice = await _make_user(db, "alice")
    cfg_path = _user_cfg_path("alice")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({
        "api_key": "sk-stale",
        "default_model": "m-stale",
        "sandbox": {"image": "python:3.12", "resource_cpu": "2"},
        "memory": {"debounce_seconds": 60},
    }))

    body = {
        "config": {
            "api_key": "sk-evil",           # 服务器管理字段 → 拒绝
            "default_model": "m-evil",      # 服务器管理字段 → 拒绝
            "memory": {"max_facts": 77},    # 白名单 → 合并
            "sandbox": {"image": "hacked"}, # 非白名单 → 忽略, 保留原值
        }
    }
    await agents_api.update_user_global_config(_Req(body), db, alice)

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "api_key" not in data
    assert "default_model" not in data
    # 白名单字段合并 (旧值 debounce_seconds 保留, 新值 max_facts 写入)
    assert data["memory"]["max_facts"] == 77
    assert data["memory"]["debounce_seconds"] == 60
    # 基础设施字段原样保留, 不被 body 覆盖
    assert data["sandbox"]["image"] == "python:3.12"
    assert data["sandbox"]["resource_cpu"] == "2"
