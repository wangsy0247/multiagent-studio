"""Tests for the harness /metrics/token-usage endpoint (usage ledger source)."""
from __future__ import annotations

import pytest

from harness.api.routers import get_token_usage
from harness.observability.usage_ledger import UsageLedger

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    ld = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "harness.observability.usage_ledger.get_usage_ledger", lambda: ld
    )
    return ld


def _seed(ledger: UsageLedger) -> None:
    ledger.record(
        user_id="u1", thread_id="t1", source="main", model="m1",
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        cache_hit_tokens=10, cache_miss_tokens=90,
    )
    ledger.record(
        user_id="u1", thread_id="t2", source="title", model="m1",
        prompt_tokens=20, completion_tokens=5, total_tokens=25,
        cache_miss_tokens=20,
    )
    ledger.record(
        user_id="u2", thread_id="t1", source="main", model="m2",
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
    )


async def test_response_shape_and_compat_keys(ledger):
    _seed(ledger)
    resp = await get_token_usage(user_id="u1", harness=None)
    # 新字段
    assert resp["prompt_tokens"] == 120
    assert resp["completion_tokens"] == 55
    assert resp["total_tokens"] == 175
    assert resp["cache_hit_tokens"] == 10
    assert resp["cache_miss_tokens"] == 110
    # 兼容旧 key (admin 页 / TokenChart)
    assert resp["total_prompt_tokens"] == 120
    assert resp["total_completion_tokens"] == 55
    assert resp["total_cost_usd"] == 0
    assert resp["by_model"]["m1"]["total_tokens"] == 175
    assert isinstance(resp["by_date"], list) and resp["by_date"]
    sources = {r["source"]: r["total_tokens"] for r in resp["by_source"]}
    assert sources == {"main": 150, "title": 25}


async def test_thread_filter(ledger):
    _seed(ledger)
    resp = await get_token_usage(user_id="u1", thread_id="t1", harness=None)
    assert resp["total_tokens"] == 150
    resp_all = await get_token_usage(user_id="u1", harness=None)
    assert resp_all["total_tokens"] == 175


async def test_user_isolation(ledger):
    _seed(ledger)
    resp = await get_token_usage(user_id="u2", harness=None)
    assert resp["total_tokens"] == 2


async def test_date_filter(ledger):
    _seed(ledger)
    resp = await get_token_usage(
        user_id="u1", start_date="2999-01-01", harness=None,
    )
    assert resp["total_tokens"] == 0
    resp = await get_token_usage(
        user_id="u1", start_date="2000-01-01", end_date="2000-01-02", harness=None,
    )
    assert resp["total_tokens"] == 0
    # 非法日期不报错, 视为无过滤
    resp = await get_token_usage(user_id="u1", start_date="not-a-date", harness=None)
    assert resp["total_tokens"] == 175
