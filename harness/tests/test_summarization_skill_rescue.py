"""Tests for SummarizationMiddleware skill rescue."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from harness.middleware.summarization import SummarizationMiddleware, SummarizationEvent

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

SKILL_PATH = "/mnt/skills/builtin/deep-research/SKILL.md"


def _make_middleware(**overrides) -> SummarizationMiddleware:
    kwargs = dict(
        model="gpt-4o-mini",
        trigger=("messages", 3),
        keep=("messages", 2),
    )
    kwargs.update(overrides)
    return SummarizationMiddleware(**kwargs)


def _skill_ai(tc_id: str = "tc1", path: str = SKILL_PATH, content: str = "") -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[{"id": tc_id, "name": "file_read", "args": {"path": path}}],
    )


def _skill_tool(tc_id: str = "tc1", content: str = "skill file body") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tc_id)


@pytest.fixture
def runtime() -> SimpleNamespace:
    return SimpleNamespace(context={"thread_id": "t1", "run_id": "r1"})


class TestFindSkillBundles:
    def test_detects_skill_read_pair(self) -> None:
        mw = _make_middleware()
        messages = [HumanMessage(content="hi"), _skill_ai(), _skill_tool()]

        bundles = mw._find_skill_bundles(messages, "/mnt/skills")

        assert len(bundles) == 1
        bundle = bundles[0]
        assert bundle.ai_index == 1
        assert bundle.skill_tool_indices == (2,)
        assert bundle.skill_tool_call_ids == frozenset({"tc1"})
        assert "deep-research" in bundle.skill_key
        assert bundle.skill_tool_tokens > 0

    def test_ignores_non_skill_paths(self) -> None:
        mw = _make_middleware()
        messages = [
            _skill_ai(path="/mnt/user-data/workspace/notes.txt"),
            _skill_tool(),
        ]
        assert mw._find_skill_bundles(messages, "/mnt/skills") == []

    def test_ignores_non_read_tool_names(self) -> None:
        mw = _make_middleware()
        ai = AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "bash", "args": {"path": SKILL_PATH}}],
        )
        assert mw._find_skill_bundles([ai, _skill_tool()], "/mnt/skills") == []

    def test_ignores_missing_path_arg(self) -> None:
        mw = _make_middleware()
        ai = AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "file_read", "args": {}}],
        )
        assert mw._find_skill_bundles([ai, _skill_tool()], "/mnt/skills") == []

    def test_is_skill_tool_call_boundary(self) -> None:
        mw = _make_middleware()
        root_call = {"id": "a", "name": "file_read", "args": {"path": "/mnt/skills"}}
        near_call = {"id": "b", "name": "file_read", "args": {"path": "/mnt/skillsx/evil"}}
        assert mw._is_skill_tool_call(root_call, "/mnt/skills") is True
        assert mw._is_skill_tool_call(near_call, "/mnt/skills") is False


class TestPartitionWithSkillRescue:
    def test_rescues_single_bundle(self) -> None:
        mw = _make_middleware()
        ai, tool = _skill_ai(), _skill_tool()
        messages = [
            HumanMessage(content="u1"),
            ai,
            tool,
            HumanMessage(content="u2"),
            AIMessage(content="working"),
            HumanMessage(content="u3"),
        ]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 4)

        # Skill AI clone (content emptied) + ToolMessage rescued into preserved.
        assert preserved[0] is not ai  # cloned
        assert isinstance(preserved[0], AIMessage)
        assert preserved[0].content == ""
        assert [tc["id"] for tc in preserved[0].tool_calls] == ["tc1"]
        assert preserved[0].id == ai.id
        assert preserved[1] is tool
        assert preserved[2:] == messages[4:]
        # Original AI had no other tool_calls and no content → nothing left behind.
        assert remaining == [messages[0], messages[3]]

    def test_splits_mixed_ai_message(self) -> None:
        mw = _make_middleware()
        ai = AIMessage(
            content="let me check",
            tool_calls=[
                {"id": "tc1", "name": "file_read", "args": {"path": SKILL_PATH}},
                {"id": "tc2", "name": "bash", "args": {"command": "ls"}},
            ],
        )
        tool_skill = _skill_tool("tc1")
        tool_bash = ToolMessage(content="ls output", tool_call_id="tc2")
        messages = [ai, tool_skill, tool_bash, HumanMessage(content="next")]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 3)

        # Preserved: AI clone with only the skill call + skill ToolMessage.
        assert [tc["id"] for tc in preserved[0].tool_calls] == ["tc1"]
        assert preserved[0].content == ""
        assert preserved[1] is tool_skill
        assert preserved[2] is messages[3]
        # Remaining: AI clone with the other call and original content + its ToolMessage.
        assert [tc["id"] for tc in remaining[0].tool_calls] == ["tc2"]
        assert remaining[0].content == "let me check"
        assert remaining[1] is tool_bash

    def test_rescues_multiple_skills_within_budget(self) -> None:
        mw = _make_middleware()
        path_b = "/mnt/skills/builtin/code-reviewer/SKILL.md"
        messages = [
            _skill_ai("tc1", SKILL_PATH),
            _skill_tool("tc1"),
            _skill_ai("tc2", path_b),
            _skill_tool("tc2"),
            HumanMessage(content="recent"),
        ]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 4)

        assert remaining == []
        assert [m.tool_call_id for m in preserved if isinstance(m, ToolMessage)] == ["tc1", "tc2"]
        assert preserved[-1] is messages[4]

    def test_drops_bundle_exceeding_per_skill_token_budget(self) -> None:
        mw = _make_middleware(preserve_recent_skill_tokens_per_skill=1)
        messages = [HumanMessage(content="u1"), _skill_ai(), _skill_tool(), HumanMessage("u2")]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 3)

        assert remaining == messages[:3]
        assert preserved == messages[3:]

    def test_drops_bundle_exceeding_total_token_budget(self) -> None:
        mw = _make_middleware(preserve_recent_skill_tokens=1)
        messages = [HumanMessage(content="u1"), _skill_ai(), _skill_tool(), HumanMessage("u2")]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 3)

        assert remaining == messages[:3]
        assert preserved == messages[3:]

    def test_deduplicates_same_skill_keeping_newest(self) -> None:
        mw = _make_middleware()
        messages = [
            _skill_ai("tc1"),
            _skill_tool("tc1"),
            HumanMessage(content="between"),
            _skill_ai("tc2"),
            _skill_tool("tc2"),
            HumanMessage(content="recent"),
        ]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 5)

        # Only the newest load of the same skill is rescued.
        assert [m.tool_call_id for m in preserved if isinstance(m, ToolMessage)] == ["tc2"]
        # The older load stays in the summarize set.
        assert [m.tool_call_id for m in remaining if isinstance(m, ToolMessage)] == ["tc1"]

    def test_count_cap_keeps_newest(self) -> None:
        mw = _make_middleware(preserve_recent_skill_count=1)
        path_b = "/mnt/skills/builtin/code-reviewer/SKILL.md"
        messages = [
            _skill_ai("tc1", SKILL_PATH),
            _skill_tool("tc1"),
            _skill_ai("tc2", path_b),
            _skill_tool("tc2"),
            HumanMessage(content="recent"),
        ]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 4)

        assert [m.tool_call_id for m in preserved if isinstance(m, ToolMessage)] == ["tc2"]
        assert [m.tool_call_id for m in remaining if isinstance(m, ToolMessage)] == ["tc1"]

    def test_disabled_with_zero_count(self) -> None:
        mw = _make_middleware(preserve_recent_skill_count=0)
        messages = [HumanMessage(content="u1"), _skill_ai(), _skill_tool(), HumanMessage("u2")]

        remaining, preserved = mw._partition_with_skill_rescue(messages, 3)

        assert remaining == messages[:3]
        assert preserved == messages[3:]


class TestMaybeSummarize:
    def test_end_to_end_rescue_and_summary(self, runtime: SimpleNamespace) -> None:
        mw = _make_middleware()
        ai, tool = _skill_ai(), _skill_tool()
        messages = [
            HumanMessage(content="u1"),
            ai,
            tool,
            HumanMessage(content="u2"),
            AIMessage(content="working"),
            HumanMessage(content="u3"),
        ]
        captured: dict[str, list] = {}
        mw._create_summary = lambda msgs: captured.setdefault("msgs", list(msgs)) or "SUMMARY"  # type: ignore[method-assign]

        result = mw._maybe_summarize({"messages": list(messages)}, runtime)

        assert result is not None
        out = result["messages"]
        assert isinstance(out[0], RemoveMessage)
        assert isinstance(out[1], HumanMessage) and out[1].name == "summary"
        # Rescued skill pair sits between the summary and the kept tail.
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "tc1" for m in out)
        # The skill ToolMessage was NOT fed to the summarizer.
        summarized = captured["msgs"]
        assert all(
            not (isinstance(m, ToolMessage) and m.tool_call_id == "tc1") for m in summarized
        )
        assert any(isinstance(m, HumanMessage) and m.content == "u1" for m in summarized)

    def test_hooks_fire_with_rescued_partition(self, runtime: SimpleNamespace) -> None:
        events: list[SummarizationEvent] = []
        mw = _make_middleware(before_summarization=[lambda ev: events.append(ev)])
        ai, tool = _skill_ai(), _skill_tool()
        messages = [
            HumanMessage(content="u1"),
            ai,
            tool,
            HumanMessage(content="u2"),
            AIMessage(content="working"),
            HumanMessage(content="u3"),
        ]
        mw._create_summary = lambda msgs: "SUMMARY"  # type: ignore[method-assign]

        result = mw._maybe_summarize({"messages": list(messages)}, runtime)

        assert result is not None
        assert len(events) == 1
        event = events[0]
        assert all(
            not (isinstance(m, ToolMessage) and m.tool_call_id == "tc1")
            for m in event.messages_to_summarize
        )
        assert any(
            isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
            for m in event.preserved_messages
        )

    def test_below_trigger_returns_none(self, runtime: SimpleNamespace) -> None:
        mw = _make_middleware()
        result = mw._maybe_summarize({"messages": [HumanMessage(content="hi")]}, runtime)
        assert result is None
