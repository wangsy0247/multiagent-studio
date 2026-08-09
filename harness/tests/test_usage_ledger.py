"""Tests for harness.observability.usage_ledger."""
from __future__ import annotations

import time

import pytest

from harness.observability.usage_ledger import (
    UsageLedger,
    UsageLedgerCallback,
    extract_usage,
    reset_usage_context,
    set_usage_context,
    usage_context,
)


# ── extract_usage ─────────────────────────────────────────────────────────


def test_extract_openai_standard_with_cache_read():
    usage = extract_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 80},
        },
        {},
    )
    assert usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cache_hit_tokens": 80,
        "cache_miss_tokens": 20,
    }


def test_extract_deepseek_explicit_cache_fields():
    usage = extract_usage(
        None,
        {
            "token_usage": {
                "prompt_tokens": 200,
                "completion_tokens": 60,
                "total_tokens": 260,
                "prompt_cache_hit_tokens": 150,
                "prompt_cache_miss_tokens": 50,
            }
        },
    )
    assert usage["prompt_tokens"] == 200
    assert usage["cache_hit_tokens"] == 150
    assert usage["cache_miss_tokens"] == 50


def test_extract_qwen_null_cached_tokens():
    usage = extract_usage(
        {"input_tokens": 16, "output_tokens": 66, "total_tokens": 82, "input_token_details": {}},
        {"token_usage": {"prompt_tokens": 16, "prompt_tokens_details": {"cached_tokens": None}}},
    )
    assert usage["cache_hit_tokens"] == 0
    assert usage["cache_miss_tokens"] == 16


def test_extract_empty():
    usage = extract_usage(None, None)
    assert usage["total_tokens"] == 0


def test_extract_raw_cached_tokens_fallback():
    usage = extract_usage(
        None,
        {
            "token_usage": {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "total_tokens": 510,
                "prompt_tokens_details": {"cached_tokens": 256},
            }
        },
    )
    assert usage["cache_hit_tokens"] == 256
    assert usage["cache_miss_tokens"] == 244


# ── UsageLedger ───────────────────────────────────────────────────────────


@pytest.fixture()
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.db")


def test_record_and_aggregate_totals(ledger):
    ledger.record(
        user_id="u1", thread_id="t1", run_id="r1", source="main", model="m1",
        prompt_tokens=100, completion_tokens=50, total_tokens=150,
        cache_hit_tokens=10, cache_miss_tokens=90,
    )
    ledger.record(
        user_id="u1", thread_id="t1", run_id="r1", source="title", model="m1",
        prompt_tokens=20, completion_tokens=5, total_tokens=25,
    )
    agg = ledger.aggregate("u1")
    assert agg["prompt_tokens"] == 120
    assert agg["completion_tokens"] == 55
    assert agg["total_tokens"] == 175
    assert agg["cache_hit_tokens"] == 10
    assert agg["cache_miss_tokens"] == 90  # 第二条记录未显式传 miss, record 不做推导


def test_aggregate_user_isolation(ledger):
    ledger.record(user_id="u1", thread_id="t1", source="main", model="m", total_tokens=10)
    ledger.record(user_id="u2", thread_id="t1", source="main", model="m", total_tokens=99)
    assert ledger.aggregate("u1")["total_tokens"] == 10
    assert ledger.aggregate("u2")["total_tokens"] == 99


def test_aggregate_thread_filter(ledger):
    ledger.record(user_id="u1", thread_id="t1", source="main", model="m", total_tokens=10)
    ledger.record(user_id="u1", thread_id="t2", source="main", model="m", total_tokens=20)
    assert ledger.aggregate("u1", thread_id="t1")["total_tokens"] == 10
    assert ledger.aggregate("u1")["total_tokens"] == 30


def test_aggregate_time_window(ledger):
    now = time.time()
    ledger.record(user_id="u1", thread_id="t1", source="main", model="m", total_tokens=10, ts=now - 100)
    ledger.record(user_id="u1", thread_id="t1", source="main", model="m", total_tokens=20, ts=now)
    assert ledger.aggregate("u1", start_ts=now - 50)["total_tokens"] == 20
    assert ledger.aggregate("u1", end_ts=now - 50)["total_tokens"] == 10


def test_aggregate_breakdowns(ledger):
    ledger.record(user_id="u1", thread_id="t1", source="main", model="m1", total_tokens=10)
    ledger.record(user_id="u1", thread_id="t1", source="memory", model="m2", total_tokens=5)
    agg = ledger.aggregate("u1")
    sources = {r["source"]: r["total_tokens"] for r in agg["by_source"]}
    models = {r["model"]: r["total_tokens"] for r in agg["by_model"]}
    assert sources == {"main": 10, "memory": 5}
    assert models == {"m1": 10, "m2": 5}
    assert len(agg["by_date"]) == 1


def test_aggregate_empty(ledger):
    agg = ledger.aggregate("nobody")
    assert agg["total_tokens"] == 0
    assert agg["by_model"] == []


# ── UsageLedgerCallback ───────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, usage_metadata=None, response_metadata=None):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _FakeGeneration:
    def __init__(self, message):
        self.message = message


class _FakeLLMResult:
    def __init__(self, message, model_name="qwen-test"):
        self.generations = [[_FakeGeneration(message)]]
        self.llm_output = {"model_name": model_name}


def _result(prompt=11, completion=7):
    return _FakeLLMResult(
        _FakeMessage(
            usage_metadata={
                "input_tokens": prompt,
                "output_tokens": completion,
                "total_tokens": prompt + completion,
            }
        )
    )


def test_callback_records_with_context(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "harness.observability.usage_ledger.get_usage_ledger", lambda: ledger
    )
    cb = UsageLedgerCallback()
    token = set_usage_context(
        {"user_id": "u1", "thread_id": "t1", "run_id": "r1", "source": "main"}
    )
    try:
        cb.on_llm_end(_result())
    finally:
        reset_usage_context(token)
    agg = ledger.aggregate("u1")
    assert agg["total_tokens"] == 18
    assert agg["by_source"][0]["source"] == "main"
    assert agg["by_model"][0]["model"] == "qwen-test"


def test_callback_skips_without_attribution(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "harness.observability.usage_ledger.get_usage_ledger", lambda: ledger
    )
    cb = UsageLedgerCallback()
    # 空 context (无 user_id/thread_id) → 不记录
    cb.on_llm_end(_result())
    assert ledger.aggregate("u1")["total_tokens"] == 0


def test_callback_skips_zero_usage(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "harness.observability.usage_ledger.get_usage_ledger", lambda: ledger
    )
    cb = UsageLedgerCallback()
    token = set_usage_context({"user_id": "u1", "thread_id": "t1", "source": "main"})
    try:
        cb.on_llm_end(_FakeLLMResult(_FakeMessage(usage_metadata=None)))
    finally:
        reset_usage_context(token)
    assert ledger.aggregate("u1")["total_tokens"] == 0


def test_usage_context_override(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "harness.observability.usage_ledger.get_usage_ledger", lambda: ledger
    )
    cb = UsageLedgerCallback()
    token = set_usage_context({"user_id": "u1", "thread_id": "t1", "source": "main"})
    try:
        with usage_context(source="title"):
            cb.on_llm_end(_result())
        cb.on_llm_end(_result())
    finally:
        reset_usage_context(token)
    sources = {r["source"]: r["total_tokens"] for r in ledger.aggregate("u1")["by_source"]}
    assert sources == {"title": 18, "main": 18}
