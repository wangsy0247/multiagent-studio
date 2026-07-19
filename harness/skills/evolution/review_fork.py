"""Background skill review fork — adapted from Hermes _spawn_background_review.

Runs after a conversation turn when the counter threshold is met.
Creates a lightweight subagent with only the ``skill_manage`` tool that
reviews the full conversation and creates / patches skills in the
background.  Completely silent — no user-visible output during execution;
results are posted to an in-memory notification queue for surfacing on
the next user turn.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from harness.models import HarnessState, SubAgentConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory notification queue — written by the review fork, read by
# the next turn's prompt builder or SSE handler.
# ---------------------------------------------------------------------------

_notifications: dict[str, list[str]] = {}  # user_id → [summary strings]


def pop_review_notifications(user_id: str) -> list[str]:
    """Pop and return accumulated review notifications for *user_id*.

    Called from ``LeadAgent.get_system_prompt()`` or the SSE handler so
    the user sees the result of the last background review.
    """
    return _notifications.pop(user_id, [])


# ---------------------------------------------------------------------------
# Review subagent identity (brief — full guidance is in SKILL_REVIEW_PROMPT)
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """\
You are a skill librarian. Your job is to review agent conversation
transcripts and maintain a library of reusable skills.

You have access to the `skill_manage` tool which supports these actions:
  - create:  Create a new skill from a full SKILL.md
  - edit:    Replace an entire existing SKILL.md
  - patch:   Partial update via append or section replacement
  - write_file: Add a support file (references/, templates/, scripts/)
  - delete:  Remove a skill entirely

Work efficiently — scan the conversation for actionable signals, then
use skill_manage to make targeted updates.  If nothing is actionable,
say "Nothing to save." and stop."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def spawn_background_review(
    messages_snapshot: list[BaseMessage],
    skill_storage: Any,
    llm_factory: Callable[..., BaseChatModel],
    model: str,
    user_id: str,
    *,
    enabled_skills: list[Any] | None = None,
    memory_context: str = "",
) -> list[str]:
    """Fork a background subagent to review the conversation and update skills.

    Called via ``asyncio.create_task()`` from the ``finally`` block of
    ``HarnessService.execute()``.  Never raises — failures are logged and
    silently discarded.

    Args:
        enabled_skills: All enabled skills visible to the parent agent
            (includes both builtin and user skills).  Passed so the review
            fork knows what the parent agent can already do.
        memory_context: ``<memory>`` block from the parent agent's context,
            containing user facts and preferences.

    Results are pushed to ``_notifications[user_id]`` for surfacing on
    the next user turn.
    """
    try:
        # ── Set provenance origin for curator management ──
        from harness.skills.evolution.provenance import ORIGIN_BACKGROUND_REVIEW, set_write_origin
        set_write_origin(ORIGIN_BACKGROUND_REVIEW)

        # ── Build task: review prompt + parent context + existing skills + conversation ──
        loaded_skill_names = _find_loaded_skills(messages_snapshot)
        existing_skills = _build_skills_catalog(
            skill_storage, user_id, loaded_skill_names,
        )
        parent_context = _format_parent_context(
            enabled_skills=enabled_skills or [],
            memory_context=memory_context,
        )
        conversation = _format_messages(messages_snapshot)
        task = _build_task(existing_skills, parent_context, conversation)

        # ── Build config ──
        config = SubAgentConfig(
            name="_bg_skill_review",
            display_name="Skill Review",
            description="Background review agent that maintains the skill library.",
            system_prompt=_REVIEW_SYSTEM_PROMPT,
            model="inherit",
            tools=None,  # passed directly
            disallowed_tools=[],
            max_turns=16,
            timeout_seconds=600,
        )

        # ── Create LLM ──
        llm = llm_factory(model)

        # ── Create skill_manage tool (per-user, gate opened for review) ──
        from harness.tools.skill_manage_tool import (
            allow_skill_manage,
            create_skill_manage_tool,
            set_skill_user_id,
        )

        set_skill_user_id(user_id)
        allow_skill_manage()  # open gate for this background task
        skill_manage = create_skill_manage_tool(
            skill_storage=skill_storage,
            model_client=llm,
        )
        skill_read = _create_skill_read_tool(skill_storage, user_id)

        # ── Run review agent ──
        logger.info(
            "Background skill review starting (user=%s, model=%s, messages=%d)",
            user_id, model, len(messages_snapshot),
        )

        review_agent = ReviewAgent(
            config=config,
            llm=llm,
            tools=[skill_read, skill_manage],
            user_id=user_id,
        )

        result = await review_agent.run(task)

        # ── Summarize actions + notify ──
        actions = _extract_actions(result)
        if actions:
            summary = " · ".join(actions)
            logger.info(
                "Background skill review completed: %d actions — %s",
                len(actions), summary,
            )
            _notifications.setdefault(user_id, []).append(summary)
        else:
            logger.info("Background skill review completed: no actions taken")

        return actions

    except Exception:
        logger.warning("Background skill review failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Task builder
# ---------------------------------------------------------------------------

from harness.skills.evolution.review_prompt import SKILL_REVIEW_PROMPT


def _build_task(existing_skills: str, parent_context: str, conversation: str) -> str:
    """Assemble the full review task."""
    parts = [SKILL_REVIEW_PROMPT]
    if parent_context:
        parts.append(parent_context)
    parts.append(existing_skills)
    parts.append(conversation)
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Parent context formatter
# ---------------------------------------------------------------------------


def _format_parent_context(
    enabled_skills: list[Any],
    memory_context: str,
) -> str:
    """Build a summary of the parent agent's context for the review fork.

    Includes:
    - All enabled skills (name + description + trigger guidance)
    - Memory facts about the user
    """
    parts: list[str] = []

    # ── Enabled skills (what the parent agent can use) ──
    if enabled_skills:
        lines = ["## Parent Agent's Available Skills\n"]
        for s in enabled_skills:
            label = "mine" if getattr(s, "user_id", None) else "built-in"
            container_path = getattr(s, "get_container_file_path", None)
            loc = container_path() if container_path else "?"
            lines.append(
                f"- **{s.name}** [{label}]: {s.description}\n  Location: {loc}"
            )
        parts.append("\n".join(lines))

    # ── Memory context ──
    if memory_context:
        parts.append(f"## User Memory (from parent agent)\n{memory_context}")

    return "\n\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Loaded-skill detection
# ---------------------------------------------------------------------------


def _find_loaded_skills(messages: list[BaseMessage]) -> set[str]:
    """Find which skills were loaded/read during the conversation.

    Scans AIMessage content and ToolMessage results for references to
    ``/mnt/skills/`` paths, extracting skill names.
    """
    import re

    loaded: set[str] = set()
    skill_path_re = re.compile(r"/mnt/skills/(?:builtin|my)/([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)")

    for msg in messages:
        content = ""
        if isinstance(msg, AIMessage):
            content = _message_content(msg)
            # Also scan tool_call args for skill paths
            for tc in getattr(msg, "tool_calls", []) or []:
                args_str = str(tc.get("args", {}))
                for m in skill_path_re.finditer(args_str):
                    loaded.add(m.group(1))
        elif isinstance(msg, ToolMessage):
            content = _message_content(msg)
        for m in skill_path_re.finditer(content):
            loaded.add(m.group(1))

    return loaded


# ---------------------------------------------------------------------------
# Existing skills summary (token-aware)
# ---------------------------------------------------------------------------

def _build_skills_catalog(
    skill_storage: Any,
    user_id: str,
    loaded_skill_names: set[str] | None = None,
) -> str:
    """Build a progressive-loading skills catalog for the review fork.

    ALL skills get name + description + file path only — NO full content
    is included inline.  The review fork uses ``skill_read`` to load a
    skill's full SKILL.md on demand, exactly like the Lead Agent uses
    ``file_read`` for progressive loading.

    Loaded skills are listed first with a ``[LOADED]`` marker so the
    review fork knows which ones are most likely to need patching.
    """
    try:
        all_skills = skill_storage.load_skills(enabled_only=False, user_id=user_id)
    except Exception:
        return "## Skills Catalog\n\n(unable to load skills)"

    user_skills = [s for s in all_skills if s.user_id]
    loaded = loaded_skill_names or set()

    if not user_skills:
        return "## Skills Catalog\n\n(no user skills yet — create the first one)"

    # Sort: loaded first, then by name
    user_skills.sort(key=lambda s: (s.name not in loaded, s.name))

    # ── 遥测: bump_use for loaded skills ──
    for skill in user_skills:
        if skill.name in loaded:
            try:
                from harness.skills.evolution.usage import bump_use
                bump_use(user_id, skill.name)
            except Exception:
                pass

    lines = [
        f"## Skills Catalog ({len(user_skills)} user skills)\n",
        "**Progressive loading**: call `skill_read(path)` to load a skill's "
        "full SKILL.md content before patching it. Do NOT patch a skill "
        "without reading it first — the catalog only shows names and "
        "descriptions.\n",
    ]

    for skill in user_skills:
        marker = " ⬅ LOADED this session" if skill.name in loaded else ""
        path = skill.get_container_file_path()
        lines.append(
            f"- **{skill.name}**{marker}: {skill.description}\n"
            f"  `skill_read(\"{path}\")`"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation formatter
# ---------------------------------------------------------------------------


# Max chars per message in the transcript before truncation.
_MAX_MESSAGE_CHARS = 3000


def _format_messages(messages: list[BaseMessage]) -> str:
    """Convert LangChain messages to a readable conversation transcript."""
    lines: list[str] = ["## Conversation Transcript\n"]
    for msg in messages:
        role = _message_role(msg)
        content = _message_content(msg)
        if not content:
            continue
        if len(content) > _MAX_MESSAGE_CHARS:
            content = content[:_MAX_MESSAGE_CHARS] + "\n... [truncated]"
        lines.append(f"### {role}\n{content}\n")
    return "\n".join(lines)


def _message_role(msg: BaseMessage) -> str:
    """Return a human-readable role label for a LangChain message."""
    if isinstance(msg, HumanMessage):
        return "User"
    if isinstance(msg, AIMessage):
        return "Assistant"
    if isinstance(msg, ToolMessage):
        return f"Tool [{msg.name}]"
    if isinstance(msg, SystemMessage):
        return "System"
    return type(msg).__name__


def _message_content(msg: BaseMessage) -> str:
    """Extract the best text representation from a message.

    For AIMessages without content (pure tool-call turns), synthesises a
    human-readable summary of the tool calls so the review agent can see
    what actions were taken — this is critical for recognising complex
    workflows that warrant skill creation.
    """
    # 1. Text content (str or list[dict])
    content = getattr(msg, "content", "")
    if isinstance(content, str) and content.strip():
        return content.strip()

    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    args = block.get("input", {})
                    parts.append(f"[tool_call: {name}({_fmt_args(args)})]")
            else:
                parts.append(str(block))

    # 2. Structured tool_calls (when content is empty/None)
    tool_calls = getattr(msg, "tool_calls", []) or []
    # Also check additional_kwargs for OpenAI-style parallel tool calls
    additional = getattr(msg, "additional_kwargs", {}) or {}
    parallel_calls = additional.get("tool_calls", []) or []

    for tc in tool_calls:
        name = tc.get("name", "unknown")
        args = tc.get("args", {})
        parts.append(f"[tool_call: {name}({_fmt_args(args)})]")

    # Deduplicate — parallel_calls may overlap with structured tool_calls
    seen = set()
    for tc in parallel_calls:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            name = fn.get("name", "unknown")
            if name not in seen:
                args_str = fn.get("arguments", "{}")
                parts.append(f"[tool_call: {name}({_truncate(args_str, 200)})]")
                seen.add(name)

    # 3. Tool results
    if isinstance(msg, ToolMessage):
        tool_output = str(content) if content else ""
        if tool_output.strip():
            parts.append(tool_output.strip())

    return " ".join(parts).strip()


def _fmt_args(args: dict | str) -> str:
    """Format tool arguments for display (truncated)."""
    s = str(args) if not isinstance(args, str) else args
    return _truncate(s, 200)


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


# ---------------------------------------------------------------------------
# Action extraction
# ---------------------------------------------------------------------------


def _extract_actions(result: str | None) -> list[str]:
    """Parse the review agent's final output for action summaries.

    Returns short human-readable labels like "Created 'foo'" or
    "Updated 'bar'".  Used for the user notification line.
    """
    if not result:
        return []

    result_lower = result.lower()

    if "nothing to save" in result_lower:
        return []

    # "Created"/"Patched"/"Updated" markers
    import re

    created = re.findall(
        r"(?:created|新增|创建)\s+(?:skill\s+)?['\"]?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)['\"]?",
        result_lower,
    )
    patched = re.findall(
        r"(?:patched|updated|修补|更新|edited)\s+(?:skill\s+)?['\"]?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)['\"]?",
        result_lower,
    )
    deleted = re.findall(
        r"(?:deleted|removed|删除)\s+(?:skill\s+)?['\"]?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)['\"]?",
        result_lower,
    )

    actions: list[str] = []
    for name in created:
        actions.append(f"Created '{name}'")
    for name in patched:
        actions.append(f"Updated '{name}'")
    for name in deleted:
        actions.append(f"Removed '{name}'")

    return actions


# ---------------------------------------------------------------------------
# Progressive-loading: skill_read tool
# ---------------------------------------------------------------------------


def _create_skill_read_tool(skill_storage: Any, user_id: str):
    """Create a ``skill_read`` tool for progressive loading of skill content.

    The review fork uses this to load a skill's full SKILL.md on demand,
    instead of having all content dumped into the task.  Directly reads
    from SkillStorage — no sandbox needed.
    """
    from langchain_core.tools import tool

    @tool
    def skill_read(path: str) -> str:
        """Read the full content of a skill's SKILL.md file.

        Use this BEFORE calling skill_manage(action='patch') to see the
        current content.  The path must be one of the locations listed
        in the Skills Catalog above.

        Args:
            path: Full path from the catalog, e.g.
                  "/mnt/skills/my/greeting-responder/SKILL.md"
        """
        # Parse skill name from path: /mnt/skills/my/<name>/SKILL.md
        # or /mnt/skills/builtin/<name>/SKILL.md
        import re

        match = re.match(r"/mnt/skills/(?:builtin|my)/([a-z0-9-]+)/SKILL\.md$", path)
        if not match:
            return (
                f"Error: invalid skill path '{path}'. "
                "Use exactly the path shown in the Skills Catalog."
            )

        skill_name = match.group(1)
        try:
            content = skill_storage.read_custom_skill(skill_name, user_id=user_id)
            return content
        except FileNotFoundError:
            return f"Error: skill '{skill_name}' not found at '{path}'."
        except Exception as exc:
            return f"Error reading skill: {exc}"

    return skill_read


# ---------------------------------------------------------------------------
# Lightweight review agent wrapper
# ---------------------------------------------------------------------------


class ReviewAgent:
    """Minimal agent wrapper for background review tasks.

    Runs directly on the calling event loop (the review fork is already a
    background ``asyncio.Task``, so LLM HTTP calls yield naturally).

    Uses a purpose-built middleware chain that does NOT include sandbox,
    thread-data, or summarization — only tool error handling and LLM
    error recovery.
    """

    def __init__(
        self,
        config: SubAgentConfig,
        llm: BaseChatModel,
        tools: list[Any],
        *,
        user_id: str = "default",
    ):
        self.config = config
        self.llm = llm
        self.tools = tools
        self.user_id = user_id

    async def run(self, task: str) -> str | None:
        """Execute the review and return the final AI response text."""
        from langchain.agents import create_agent
        from langchain.agents.middleware import AgentMiddleware

        from harness.middleware.llm_error import LLMErrorHandlingMiddleware
        from harness.middleware.tool_error import ToolErrorHandlingMiddleware

        # ── Minimal middleware chain — NO sandbox, NO thread_data ──
        # The review fork only calls skill_manage (which writes to host
        # filesystem via SkillStorage).  No sandbox operations needed.
        middlewares: list[AgentMiddleware] = [
            LLMErrorHandlingMiddleware(),
            ToolErrorHandlingMiddleware({"max_retries": 2}),
        ]

        agent = create_agent(
            model=self.llm,
            tools=list(self.tools),
            system_prompt=None,  # injected via messages below
            middleware=middlewares,
            state_schema=HarnessState,
        )

        messages: list[BaseMessage] = [
            SystemMessage(content=self.config.system_prompt),
            HumanMessage(content=task),
        ]

        run_config = {
            "recursion_limit": self.config.max_turns,
            "configurable": {"thread_id": f"_bg_review_{self.user_id}"},
        }

        final_content: str | None = None
        try:
            async for chunk in agent.astream(
                {"messages": messages},
                config=run_config,
                stream_mode="values",
            ):
                if "messages" in chunk:
                    last_msg = chunk["messages"][-1]
                    if isinstance(last_msg, AIMessage) and last_msg.content:
                        final_content = str(last_msg.content)
        except Exception:
            logger.warning("Review agent stream failed", exc_info=True)

        return final_content
