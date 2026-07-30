"""Lead Agent — multi-agent orchestration core with DeerFlow-style prompt.

The LeadAgent is a *configuration provider* for ``create_agent()`` — it builds
the system prompt and tool list.  The actual ReAct loop is handled by
``create_agent()`` internally, wrapped by the outer ``HarnessGraphFactory``.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool

from harness.agents.presets import PRESET_SUBAGENTS
from harness.tools.builtins import build_lead_tools
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Prompt template — DeerFlow-aligned XML-structured system prompt
# ──────────────────────────────────────────────────────────────────────────────


def _build_subagent_section(max_concurrent: int) -> str:
    """Build the subagent system prompt section with dynamic concurrency limit."""
    n = max_concurrent
    agent_descriptions = "\n".join(
        f"- **{name}**: {info['display_name']} — {info['description']}"
        for name, info in PRESET_SUBAGENTS.items()
    )
    return f"""<subagent_system>
You have access to specialized subagents that can execute tasks in parallel. Subagents are a **tool to use when beneficial**, not a mandatory workflow — complete tasks directly whenever that is simpler and faster.

**Default: Execute Directly**

Most tasks do NOT need subagents. Use your own tools to handle the request directly. Subagents add coordination overhead — only pay that cost when it buys you genuine parallelism or specialization.

**Available Subagents:**
{agent_descriptions}

**When to Use Subagents:**

✅ Use subagents when:
- **Truly parallel work**: 2+ independent tasks that can run simultaneously for a meaningful speedup
- **Specialization matters**: A task needs a specific subagent's unique toolset (e.g., coder for complex code execution, researcher for multi-source literature search)
- **Heavy-lift tasks**: A single large task benefits from being split into parallel sub-tasks

❌ Do NOT use subagents when:
- **Simple / single-step tasks**: "Read this file", "Search for X" — just do it yourself
- **Sequential work**: Each step depends on the previous result — do the steps yourself in order
- **Need clarification first**: Must ask the user before proceeding
- **Conversational / meta questions**: "What did we talk about?"

**Concurrency:** Maximum **{n} `task` calls per response**. For >{n} genuinely parallel tasks, split into batches across turns.

**You Own the Thinking — SubAgents Own the Execution**

You are responsible for understanding the user's intent, analyzing trade-offs, and making decisions. SubAgents only execute within the constraints you set. Never outsource judgment calls — "which library should I use" or "how should I refactor this" are your decisions, not the subagent's.

**Writing Good Instructions**

SubAgents have **no access to this conversation**. Every instruction must be fully self-contained. Before calling, clarify these five points in your thinking:

1. **Goal** — What exactly to deliver? (fact list? code change? verification report?)
2. **Background** — What context does the subagent need? (tech stack, current situation, constraints)
3. **Scope** — Which files / directories / line numbers? What has already been ruled out?
4. **Constraints** — READ-ONLY or allowed to modify? If modifying, which files exactly?
5. **Format** — What structure and length? (e.g. "within 200 words, list key findings only")

If any of these are unclear, ask the user before calling the subagent.

Structure your instruction with: 【Goal】【Background】【Scope】【Constraints】【Output Format】. Give exact file paths and line numbers whenever possible. Common mistakes to avoid: outsourcing decisions ("fix the bug" without telling which bug), no file paths, not specifying read-only vs writable, no length limit, not listing already-excluded areas.

**Verify After Every SubAgent Call**

Subagent summaries describe what they **intended** to do — not always what they **actually** did. After every call:
- If code was modified → use file_read to check the actual file content
- If facts were reported → spot-check 1-2 key claims
- If recommendations were made → evaluate independently, don't blindly accept
- If multiple subagents returned results → cross-check for contradictions

**Example — Good vs Bad Instruction:**

❌ BAD: `task(agent_name="coder", instruction="Fix the login bug")`
→ Subagent doesn't know which bug, which file, what constraints.

✅ GOOD:
```
【Goal】Fix OAuth token refresh failure — users get logged out ~1 hour after login.

【Background】Express + passport-saml project. Token refresh logic modified 3 days ago,
likely regression. access_token valid 3600s, refresh at 300s before expiry.

【Scope】Focus on src/auth/middleware.ts L78-120 (verifyToken) and
src/auth/tokenManager.ts (refreshToken). Ruled out: DB pool, SAML config, frontend.

【Constraints】READ-ONLY first — analyze and report root cause + suggested fix.
Do NOT modify code until I confirm.

【Output Format】Within 200 words: ① Root cause ② Files involved ③ Suggested fix.
```
</subagent_system>"""

def _build_clarification_section() -> str:
    return """<clarification_system>
**WORKFLOW PRIORITY: CLARIFY → PLAN → ACT**

1. **FIRST**: Analyze the request in your thinking — identify what's unclear, missing, or ambiguous
2. **SECOND**: If clarification is needed, call `ask_clarification` tool IMMEDIATELY — do NOT start working
3. **THIRD**: Only after all clarifications are resolved, proceed with planning and execution

**CRITICAL RULE: Clarification ALWAYS comes BEFORE action. Never start working and clarify mid-execution.**

**MANDATORY Clarification Scenarios — You MUST call ask_clarification BEFORE starting work when:**

1. **Missing Information** (`missing_info`): Required details not provided
   - Example: User says "create a web scraper" but doesn't specify the target website
   - Example: "Deploy the app" without specifying environment
   - **REQUIRED ACTION**: Call ask_clarification to get the missing information

2. **Ambiguous Requirements** (`ambiguous_requirement`): Multiple valid interpretations exist
   - Example: "Optimize the code" could mean performance, readability, or memory usage
   - Example: "Make it better" is unclear what aspect to improve
   - **REQUIRED ACTION**: Call ask_clarification to clarify the exact requirement

3. **Approach Choices** (`approach_choice`): Several valid approaches exist
   - Example: "Add authentication" could use JWT, OAuth, session-based, or API keys
   - Example: "Store data" could use database, files, cache, etc.
   - **REQUIRED ACTION**: Call ask_clarification to let user choose the approach

4. **Risky Operations** (`risk_confirmation`): Destructive actions need confirmation
   - Example: Deleting files, modifying production configs, database operations
   - Example: Overwriting existing code or data
   - **REQUIRED ACTION**: Call ask_clarification to get explicit confirmation

5. **Suggestions** (`suggestion`): You have a recommendation but want approval
   - Example: "I recommend refactoring this code. Should I proceed?"
   - **REQUIRED ACTION**: Call ask_clarification to get approval

**STRICT ENFORCEMENT:**
- ❌ DO NOT start working and then ask for clarification mid-execution — clarify FIRST
- ❌ DO NOT skip clarification for "efficiency" — accuracy matters more than speed
- ❌ DO NOT make assumptions when information is missing — ALWAYS ask
- ❌ DO NOT proceed with guesses — STOP and call ask_clarification first
- ✅ Analyze the request in thinking → Identify unclear aspects → Ask BEFORE any action
- ✅ If you identify the need for clarification in your thinking, you MUST call the tool IMMEDIATELY
- ✅ After calling ask_clarification, execution will be interrupted automatically
- ✅ Wait for user response — do NOT continue with assumptions

**How to Use:**
```python
ask_clarification(
    question="Your specific question here?",
    clarification_type="missing_info",  # or other type
    context="Why you need this information",  # optional but recommended
    options=["option1", "option2"]  # optional, for choices
)
```

**Example:**
User: "Deploy the application"
You (thinking): Missing environment info — I MUST ask for clarification
You (action): ask_clarification(
    question="Which environment should I deploy to?",
    clarification_type="approach_choice",
    context="I need to know the target environment for proper configuration",
    options=["development", "staging", "production"]
)
[Execution stops — wait for user response]

User: "staging"
You: "Deploying to staging..." [proceed]
</clarification_system>"""


def _build_working_directory_section() -> str:
    return """<working_directory>
- User uploads: `/mnt/user-data/uploads` — Files uploaded by the user (automatically listed in context)
- User workspace: `/mnt/user-data/workspace` — Working directory for temporary files
- Output files: `/mnt/user-data/outputs` — Final deliverables must be saved here

**File Management:**
- Uploaded files are automatically listed in the <uploaded_files> section before each request
- Use `file_read` tool to read uploaded files using their paths from the list
- For PDF, PPT, Excel, and Word files, converted Markdown versions (*.md) are available alongside originals
- All temporary work happens in `/mnt/user-data/workspace`
- Treat `/mnt/user-data/workspace` as your default current working directory for coding and file-editing tasks
- When writing scripts or commands that create/read files from the workspace, prefer relative paths such as `hello.txt`, `../uploads/data.csv`, and `../outputs/report.md`
- Avoid hardcoding `/mnt/user-data/...` inside generated scripts when a relative path from the workspace is enough
- Final deliverables must be copied to `/mnt/user-data/outputs`
</working_directory>"""


def _build_citations_section() -> str:
    """Build the citations guidance section for web search results."""
    return """<citations>
**CRITICAL: Always include citations when using web search results**

- **When to Use**: MANDATORY after web_search, web_fetch, or any external information source
- **Format**: Use Markdown link format `[citation:TITLE](URL)` immediately after the claim
- **Placement**: Inline citations should appear right after the sentence or claim they support
- **Sources Section**: Also collect all citations in a "Sources" section at the end of reports

**Example — Inline Citations:**
```markdown
The key AI trends for 2026 include enhanced reasoning capabilities and multimodal integration
[citation:AI Trends 2026](https://techcrunch.com/ai-trends).
Recent breakthroughs in language models have also accelerated progress
[citation:OpenAI Research](https://openai.com/research).
```

**Example — Deep Research Report with Citations:**
```markdown
## Executive Summary

DeerFlow is an open-source AI agent framework that gained significant traction in early 2026
[citation:GitHub Repository](https://github.com/bytedance/deer-flow). The project focuses on
providing a production-ready agent system with sandbox execution and memory management
[citation:DeerFlow Documentation](https://deer-flow.dev/docs).

## Key Analysis

### Architecture Design

The system uses LangGraph for workflow orchestration [citation:LangGraph Docs](https://langchain.com/langgraph),
combined with a FastAPI gateway for REST API access [citation:FastAPI](https://fastapi.tiangolo.com).

## Sources

### Primary Sources
- [GitHub Repository](https://github.com/bytedance/deer-flow) - Official source code and documentation
- [DeerFlow Documentation](https://deer-flow.dev/docs) - Technical specifications

### Media Coverage
- [AI Trends 2026](https://techcrunch.com/ai-trends) - Industry analysis
```

**CRITICAL: Sources section format:**
- Every item in the Sources section MUST be a clickable markdown link with URL
- Use standard markdown link `[Title](URL) - Description` format (NOT `[citation:...]` format)
- The `[citation:Title](URL)` format is ONLY for inline citations within the report body
- ❌ WRONG: `GitHub 仓库 - 官方源代码和文档` (no URL!)
- ❌ WRONG in Sources: `[citation:GitHub Repository](url)` (citation prefix is for inline only!)
- ✅ RIGHT in Sources: `[GitHub Repository](https://github.com/...) - 官方源代码和文档`

**WORKFLOW for Research Tasks:**
1. Use web_search to find sources → Extract {{title, url, snippet}} from results
2. Write content with inline citations: `claim [citation:Title](url)`
3. Collect all citations in a "Sources" section at the end
4. NEVER write claims without citations when sources are available

**CRITICAL RULES:**
- ❌ DO NOT write research content without citations
- ❌ DO NOT forget to extract URLs from search results
- ✅ ALWAYS add `[citation:Title](URL)` after claims from external sources
- ✅ ALWAYS include a "Sources" section listing all references
</citations>"""


def _build_response_style_section() -> str:
    return """<response_style>
- Clear and Concise: Avoid over-formatting unless requested
- Natural Tone: Use paragraphs and prose, not bullet points by default
- Action-Oriented: Focus on delivering results, not explaining processes
</response_style>"""


def _build_critical_reminders_section(max_concurrent: int, subagent_enabled: bool) -> str:
    n = max_concurrent
    subagent_reminder = (
        f"- **Subagents**: You have subagent capabilities. Use them when tasks are genuinely parallel; "
        f"otherwise handle work directly. Max {n} `task` calls per response.\n"
        if subagent_enabled
        else ""
    )
    return f"""<critical_reminders>
- **Clarification First**: ALWAYS clarify unclear/missing/ambiguous requirements BEFORE starting work — never assume or guess
{subagent_reminder}- Output Files: Final deliverables must be in `/mnt/user-data/outputs`
- Clarity: Be direct and helpful, avoid unnecessary meta-commentary
- Including Images and Mermaid: Images and Mermaid diagrams are always welcomed in the Markdown format, and you're encouraged to use `![Image Description](image_path)` or "```mermaid" to display images in response or Markdown files
- Multi-task: Better utilize parallel tool calling to call multiple tools at one time for better performance
- Language Consistency: Keep using the same language as user's
- Always Respond: Your thinking is internal. You MUST always provide a visible response to the user after thinking.
</critical_reminders>"""


# ──────────────────────────────────────────────────────────────────────────────
# Skills prompt section (imported from harness.skills to avoid circular imports)
# ──────────────────────────────────────────────────────────────────────────────

from harness.skills.prompt import get_skills_prompt_section  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Master template
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """<role>
You are {agent_name}, an AI assistant with multi-agent orchestration capabilities.
</role>

{agent_soul}
<language>
{language_section}
</language>

<thinking_style>
- Think concisely and strategically about the user's request BEFORE taking action
- Break down the task: What is clear? What is ambiguous? What is missing?
- **PRIORITY CHECK: If anything is unclear, missing, or has multiple interpretations, you MUST ask for clarification FIRST — do NOT proceed with work**
{subagent_thinking}- Never write down your full final answer or report in thinking process, but only outline
- CRITICAL: After thinking, you MUST provide your actual response to the user. Thinking is for planning, the response is for delivery.
- Your response must contain the actual answer, not just a reference to what you thought about
</thinking_style>

{clarification_section}

{subagent_section}

{working_directory_section}

{skills_section}

{review_note}

{citations_section}

{response_style_section}

{critical_reminders_section}"""


def _build_language_section() -> str:
    """Build a language directive based on persisted user preference.

    Checks the user's memory for a language preference fact. If the user
    has consistently communicated in a specific language, return an
    explicit instruction to use that language. Otherwise, fall back to
    matching the current conversation's language.
    """
    try:
        from harness.memory.updater import get_memory_data
        memory = get_memory_data(agent_name=None)
        facts = memory.get("facts", []) if isinstance(memory, dict) else []

        # Search for language-preference facts.
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            content = str(fact.get("content", "")).lower()
            if fact.get("category") == "preference" and any(
                kw in content for kw in ("language", "prefers chinese", "uses chinese", "中文", "mandarin", "speaks chinese")
            ):
                if "chinese" in content or "中文" in content:
                    return (
                        "The user prefers communicating in Chinese (中文). "
                        "You MUST think in Chinese and respond in Chinese. "
                        "Use Chinese for all output unless the user explicitly switches languages."
                    )
                if "english" in content:
                    return (
                        "The user prefers communicating in English. "
                        "You MUST think in English and respond in English."
                    )

        # Fallback: check personalContext for language clues.
        personal = (
            memory.get("user", {}).get("personalContext", {}).get("summary", "")
            if isinstance(memory, dict)
            else ""
        )
        if personal:
            personal_lower = personal.lower()
            if any(kw in personal_lower for kw in ("chinese", "中文", "mandarin")):
                return (
                    "The user prefers communicating in Chinese (中文). "
                    "You MUST think in Chinese and respond in Chinese."
                )
            if "english" in personal_lower:
                return (
                    "The user prefers communicating in English. "
                    "You MUST think in English and respond in English."
                )

    except Exception:
        pass

    # Default: match the user's language in the current conversation.
    return (
        "Match the language the user is currently using. "
        "If the user writes in Chinese, respond in Chinese. "
        "If the user writes in English, respond in English."
    )


def apply_prompt_template(
    agent_name: str = "Multi-Agent Orchestrator",
    max_concurrent_subagents: int = 3,
    subagent_enabled: bool = True,
    *,
    agent_soul: str = "",
    skills_section: str = "",
    language_section: str = "",
    review_note: str = "",
) -> str:
    """Assemble the full Lead Agent system prompt from sections.

    Args:
        agent_name: Display name for the agent
        max_concurrent_subagents: Max parallel task calls
        subagent_enabled: Whether subagent orchestration is available
        agent_soul: Agent personality / behavioral definition (SOUL.md content)
        skills_section: Pre-built ``<skill_system>`` XML block (or empty string)
    """
    n = max_concurrent_subagents

    # Subagent thinking guidance (injected into thinking_style)
    subagent_thinking = (
        f"- **Subagent check: If the task has genuinely independent parallel parts, consider subagents. "
        f"Otherwise, just handle it directly. "
        f"If using subagents, max {n} `task` calls per response.**\n"
        if subagent_enabled
        else ""
    )

    # Build sections
    subagent_section = _build_subagent_section(n) if subagent_enabled else ""
    clarification_section = _build_clarification_section()
    working_directory_section = _build_working_directory_section()
    citations_section = _build_citations_section()
    response_style_section = _build_response_style_section()
    critical_reminders_section = _build_critical_reminders_section(n, subagent_enabled)

    # 包装 SOUL: 有内容时加上 XML 标签，没有则完全占位为空
    soul_block = f"<soul>\n{agent_soul}\n</soul>" if agent_soul else ""

    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name,
        agent_soul=soul_block,
        language_section=language_section or _build_language_section(),
        subagent_thinking=subagent_thinking,
        clarification_section=clarification_section,
        subagent_section=subagent_section,
        working_directory_section=working_directory_section,
        skills_section=skills_section,
        review_note=review_note,
        citations_section=citations_section,
        response_style_section=response_style_section,
        critical_reminders_section=critical_reminders_section,
    )


# ──────────────────────────────────────────────────────────────────────────────
# LeadAgent class
# ──────────────────────────────────────────────────────────────────────────────


class LeadAgent:
    """Lead Agent — configuration provider for ``create_agent()``.

    Builds the system prompt and tool list.  The actual ReAct execution loop
    is handled by ``create_agent()`` (via ``HarnessGraphFactory``), so there
    is no ``run()`` method — the agent is driven entirely by the graph.

    Responsibilities:
    - Understand user intent and plan task decomposition
    - Decide when to create SubAgents and what type
    - Orchestrate SubAgent execution order (serial / parallel)
    - Integrate SubAgent results into a coherent final answer
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        subagent_manager: Any | None = None,
        agent_name: str = "Multi-Agent Orchestrator",
        max_concurrent_subagents: int = 3,
        config_manager: ConfigManager | None = None,
        *,
        skill_storage: Any | None = None,
        agent_config: Any | None = None,
        user_id: str | None = None,
        agent_soul: str = "",
    ):
        self.tool_registry = tool_registry
        self.subagent_manager = subagent_manager
        self.agent_name = agent_name
        self.max_concurrent = max_concurrent_subagents
        self.config_manager = config_manager
        self.skill_storage = skill_storage
        self.agent_config = agent_config
        self._user_id = user_id
        self._agent_soul = agent_soul

    # ------------------------------------------------------------------
    # system prompt
    # ------------------------------------------------------------------

    def _available_skill_names(self) -> set[str] | None:
        """Return the skill whitelist from agent config.

        * ``None`` — load all enabled skills (legacy / default behaviour).
        * ``set()`` (empty) — load no skills.
        * ``{"a", "b"}`` — only load the named skills.
        """
        if self.agent_config is not None:
            skills_attr = getattr(self.agent_config, "skills", None)
            if skills_attr is not None:
                return set(skills_attr)
        return None

    def get_system_prompt(self) -> str:
        """Build the complete system prompt template, including enabled skills."""
        from harness.config.memory_config import get_memory_config
        mem_cfg = get_memory_config()

        # Build skills section from enabled skills.
        skills_section = ""
        if self.skill_storage is not None:
            try:
                enabled_skills = self.skill_storage.load_skills(
                    enabled_only=True, user_id=self._user_id,
                )

                # Apply per-agent skills whitelist
                whitelist = self._available_skill_names()
                if whitelist is not None:
                    enabled_skills = [
                        s for s in enabled_skills if s.name in whitelist
                    ]

                if enabled_skills:
                    # Try cache first
                    try:
                        from harness.skills.cache import (
                            build_skills_signature,
                            get_cached_skills_prompt_section,
                        )
                        sig = build_skills_signature(enabled_skills)
                        skills_section = get_cached_skills_prompt_section(
                            sig,
                            lambda: get_skills_prompt_section(enabled_skills),
                        )
                    except Exception:
                        skills_section = get_skills_prompt_section(enabled_skills)
            except Exception:
                logger.exception("Failed to load skills for system prompt")

        # Check for background skill review results from the previous turn
        review_note = ""
        try:
            from harness.skills.evolution.review_fork import pop_review_notifications

            if self._user_id:
                notifications = pop_review_notifications(self._user_id)
                if notifications:
                    review_note = (
                        "<review_updates>\n"
                        "Background skill review completed last session: "
                        + " · ".join(notifications)
                        + "\n</review_updates>"
                    )
        except Exception:
            pass

        return apply_prompt_template(
            agent_name=self.agent_name,
            max_concurrent_subagents=self.max_concurrent,
            subagent_enabled=self.subagent_manager is not None,
            agent_soul=self._agent_soul,
            skills_section=skills_section,
            review_note=review_note,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def build_tools(self) -> list[BaseTool]:
        """Return all tools for the Lead Agent.

        Lead Agent tools are configured in ``config.yaml`` under
        ``lead_agent.tools``.  Each entry can be either an individual
        tool name or a tool group name.  Only the orchestration tools
        (Agent, ask_clarification) are always present.

        Example config::

            lead_agent:
              tools:
                - web_search       # single tool
                - search           # group → all tools in "search" group
                - files            # group → all tools in "files" group
        """
        tools: list[BaseTool] = build_lead_tools(
            self.subagent_manager,
            parent_skills=(
                list(self._available_skill_names())
                if self._available_skill_names() is not None
                else None
            ),
        )
        seen: set[str] = {t.name for t in tools}

        if self.config_manager:
            lead_cfg: dict = self.config_manager.get("lead_agent") or {}
            entries: list[str] = lead_cfg.get("tools", [])
            if isinstance(entries, list):
                for entry in entries:
                    if self.tool_registry.has_tool(entry):
                        # ── single tool name ──
                        t = self.tool_registry.get_tool(entry)
                        if t.name not in seen:
                            tools.append(t)
                            seen.add(t.name)
                    else:
                        # ── might be a group name ──
                        group_tools = self.tool_registry.get_tools_by_category(entry)
                        if group_tools:
                            for t in group_tools:
                                if t.name not in seen:
                                    tools.append(t)
                                    seen.add(t.name)
                        else:
                            logger.warning(
                                "Lead Agent tool/group '%s' not found in registry",
                                entry,
                            )

        # ── Apply skill allowed-tools filtering ──
        if self.skill_storage is not None:
            try:
                enabled_skills = self.skill_storage.load_skills(
                    enabled_only=True, user_id=self._user_id,
                )

                # Apply per-agent skills whitelist
                whitelist = self._available_skill_names()
                if whitelist is not None:
                    enabled_skills = [
                        s for s in enabled_skills if s.name in whitelist
                    ]

                if enabled_skills:
                    from harness.skills.tool_policy import filter_tools_by_skill_allowed_tools

                    before = len(tools)
                    tools = filter_tools_by_skill_allowed_tools(tools, enabled_skills)
                    logger.debug(
                        "Skill tool-policy filtered %d → %d tools",
                        before,
                        len(tools),
                    )
            except Exception:
                logger.exception("Failed to apply skill tool-policy")

        return tools


# ──────────────────────────────────────────────────────────────────────────────
# Middleware builder + app factory (added during DeerFlow-aligned refactor)
# ──────────────────────────────────────────────────────────────────────────────

from harness.config.config_manager import ConfigManager
from harness.middleware.clarification import ClarificationMiddleware
from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
from harness.middleware.dynamic_context import DynamicContextMiddleware
from harness.middleware.guardrail import GuardrailMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.summarization import create_summarization_middleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.title import TitleMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.uploads import UploadsMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.sandbox import SandboxMiddleware
from harness.models import HarnessState
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig


def _get_runtime_config(config: RunnableConfig) -> dict:
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg


def _build_middlewares(
    config: RunnableConfig,
    *,
    config_manager: ConfigManager | None = None,
    agent_name: str | None = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
) -> list[AgentMiddleware]:
    """Build the full 20-middleware chain matching DeerFlow order."""
    middlewares: list[AgentMiddleware] = []
    cfg = _get_runtime_config(config)
    workspace_root = str(cfg.get("workspace_root", ""))
    is_plan_mode = cfg.get("is_plan_mode", False)
    subagent_enabled = cfg.get("subagent_enabled", False)
    max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
    memory_enabled = True; summarization_enabled = False; guardrail_enabled = False
    vision_enabled = cfg.get("vision_enabled", False)
    tool_search_enabled = cfg.get("tool_search_enabled", False)
    tool_max_retries = cfg.get("tool_max_retries", 3)
    auto_title_enabled = cfg.get("auto_title", False)
    title_model = cfg.get("title_model", "gpt-4o-mini")
    summary_model = cfg.get("summary_model", "")
    api_key = cfg.get("openai_api_key", ""); base_url = cfg.get("openai_base_url", "")
    user_id = cfg.get("user_id", "default")
    if config_manager is not None:
        try:
            mem_cfg = config_manager.get("memory")
            if isinstance(mem_cfg, dict): memory_enabled = mem_cfg.get("enabled", True)
        except Exception: pass
        try:
            summ_cfg = config_manager.get("summarization")
            if isinstance(summ_cfg, dict): summarization_enabled = summ_cfg.get("enabled", False)
        except Exception: pass
        try:
            guard_cfg = config_manager.get("guardrails")
            if isinstance(guard_cfg, dict): guardrail_enabled = guard_cfg.get("enabled", False)
        except Exception: pass
        try:
            ts_cfg = config_manager.get("tool_search")
            if isinstance(ts_cfg, dict): tool_search_enabled = ts_cfg.get("enabled", False)
        except Exception: pass
    # loop_detection config (from config.yaml)
    loop_cfg: dict = {}
    if config_manager is not None:
        try:
            raw_loop = config_manager.get("loop_detection")
            if isinstance(raw_loop, dict):
                loop_cfg = raw_loop
        except Exception:
            pass
    middlewares.append(ThreadDataMiddleware({"workspace_root": workspace_root}))
    middlewares.append(UploadsMiddleware())
    middlewares.append(SandboxMiddleware())
    middlewares.append(DanglingToolCallMiddleware())
    middlewares.append(LLMErrorHandlingMiddleware())
    if guardrail_enabled: middlewares.append(GuardrailMiddleware())
    middlewares.append(SandboxAuditMiddleware())
    middlewares.append(ToolErrorHandlingMiddleware({"max_retries": tool_max_retries}))
    middlewares.append(DynamicContextMiddleware(agent_name=agent_name))
    if summarization_enabled:
        # 不再挂 memory_flush_hook: MemoryMiddleware 每轮已增量提交最新交换,
        # 被压缩的旧消息在其所属轮次就已入队, 无需压缩前抢救.
        summ_mw = create_summarization_middleware(
            model_name=summary_model,
            api_key=api_key,
            base_url=base_url,
            user_id=user_id,
        )
        if summ_mw is not None: middlewares.append(summ_mw)
    if is_plan_mode: middlewares.append(TodoMiddleware())
    middlewares.append(TokenUsageMiddleware())
    if auto_title_enabled:
        middlewares.append(TitleMiddleware({"title_model": title_model, "api_key": api_key, "base_url": base_url, "user_id": user_id}))
    if memory_enabled:
        middlewares.append(MemoryMiddleware(
            {"openai_api_key": api_key, "openai_base_url": base_url,
             "memory_model": cfg.get("memory_model", "")},
            agent_name=agent_name,
        ))
    if vision_enabled: middlewares.append(ViewImageMiddleware())
    if tool_search_enabled: middlewares.append(DeferredToolFilterMiddleware())
    if subagent_enabled: middlewares.append(SubagentLimitMiddleware({"max_concurrent": max_concurrent_subagents}))
    middlewares.append(LoopDetectionMiddleware(
        loop_cfg,
        warn_threshold=loop_cfg.get("warn_threshold", 3),
        hard_limit=loop_cfg.get("hard_limit", 5),
        tool_freq_warn=loop_cfg.get("tool_freq_warn", 20),
        tool_freq_hard_limit=loop_cfg.get("tool_freq_hard_limit", 30),
        window_size=loop_cfg.get("window_size", 35),
    ))
    middlewares.append(SafetyFinishReasonMiddleware())
    if custom_middlewares: middlewares.extend(custom_middlewares)
    middlewares.append(ClarificationMiddleware())
    return middlewares


def make_lead_agent(config: RunnableConfig, *, config_manager: ConfigManager | None = None):
    """LangGraph graph factory from config.yaml."""
    cfg = _get_runtime_config(config)
    agent_name = cfg.get("agent_name")
    model_name = cfg.get("model_name") or cfg.get("model", "gpt-4o")
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model=model_name, api_key=cfg.get("openai_api_key", ""),
                     base_url=cfg.get("openai_base_url", ""), temperature=0.3,
                     request_timeout=120, max_retries=2)
    tools = cfg.get("_tools", [])
    system_prompt = cfg.get("_system_prompt", "You are a helpful assistant.")
    middlewares = _build_middlewares(config, config_manager=config_manager, agent_name=agent_name)
    return create_agent(model=llm, tools=tools or None, middleware=middlewares,
                        system_prompt=system_prompt, state_schema=HarnessState)
