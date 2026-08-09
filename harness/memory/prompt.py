"""Prompt templates for memory update and injection — adapted from harness."""

import math
import re
from typing import Any

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

# ── Memory update prompt (identical to the canonical format) ──────────────────────────
MEMORY_UPDATE_PROMPT = """You are a memory management system. Your task is to analyze a conversation and update the user's memory profile.

Current Memory State:
<current_memory>
{current_memory}
</current_memory>

New Conversation to Process:
<conversation>
{conversation}
</conversation>

Instructions:
1. Analyze the conversation for important information about the user
2. Extract relevant facts, preferences, and context with specific details (numbers, names, technologies)
3. Update the memory sections as needed following the detailed length guidelines below

Before extracting facts, perform a structured reflection on the conversation:
1. Error/Retry Detection: Did the agent encounter errors, require retries, or produce incorrect results?
   If yes, record the root cause and correct approach as a high-confidence fact with category "correction".
2. User Correction Detection: Did the user correct the agent's direction, understanding, or output?
   If yes, record the correct interpretation or approach as a high-confidence fact with category "correction".
   Include what went wrong in "sourceError" only when category is "correction" and the mistake is explicit in the conversation.
3. Project Constraint Discovery: Were any project-specific constraints discovered during the conversation?
   If yes, record them as facts with the most appropriate category and confidence.

{correction_hint}

Memory Section Guidelines:

**User Context** (Current state - concise summaries):
- workContext: Professional role, company, key projects, main technologies (2-3 sentences)
- personalContext: Languages, communication preferences, key interests (1-2 sentences). CRITICAL: explicitly note the user's preferred spoken/written language (e.g., "prefers Chinese", "uses English", "mixes Chinese and English"). When the user consistently writes in a particular language, treat this as a high-confidence preference.
- topOfMind: Multiple ongoing focus areas and priorities (3-5 sentences, detailed paragraph)
- avoidances: Pet peeves, communication style to avoid, things that annoy the user (1-2 sentences). CRITICAL: if the user explicitly says "don't do X", "stop doing Y", or expresses frustration, record it here.

**History** (Temporal context - rich paragraphs):
- recentWeeks: Detailed summary of activities in the past few weeks (4-6 sentences or 1-2 paragraphs)
- earlierContext: Important historical patterns (3-5 sentences or 1 paragraph)
- longTermBackground: Persistent background and foundational context (2-4 sentences)

**Facts Extraction**:
- Extract specific, quantifiable details
- Include proper nouns (company names, project names, technology names)
- CRITICAL: detect the user's preferred language — if the user consistently writes in Chinese, record as fact: "User prefers communicating in Chinese" with category "preference" and confidence 0.95+
- **Tool & Technique facts**: Record discovered tool quirks, CLI flags that actually work, effective patterns, and pitfalls with their workarounds. Example: "docker commands don't need sudo — user is already in the docker group" → technique, confidence 0.95. These are high-value facts because they prevent the agent from repeating mistakes.
- **7-day value filter**: Do NOT record facts that will be stale within a week. Task progress ("fixed bug X"), PR numbers, commit SHAs, "Phase N done", temporary file paths, session outcomes — these belong in session history, not memory. Ask yourself: "Will this fact still matter 7 days from now?" Only record it if the answer is yes.
- Categories: preference, knowledge, context, behavior, goal, correction, technique
- Confidence levels: 0.9-1.0 explicit, 0.7-0.8 strongly implied, 0.5-0.6 inferred patterns

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }},
    "avoidances": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentWeeks": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "newFacts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ],
  "factsToRemove": ["fact_id_1", "fact_id_2"]
}}

Important Rules:
- Only set shouldUpdate=true if there's meaningful new information
- Follow length guidelines: workContext/personalContext/avoidances are concise (1-2 sentences), topOfMind and history are detailed
- Only add facts that are clearly stated (0.9+) or strongly implied (0.7+)
- Use category "correction" for explicit agent mistakes; assign confidence >= 0.95
- Use category "technique" for discovered tool quirks, effective patterns, and workarounds; assign confidence >= 0.95 (agent's own experience is authoritative)
- Apply the 7-day filter: if a fact will be stale in a week, do NOT record it
- Remove facts that are contradicted by new information
- IMPORTANT: Do NOT record file upload events in memory.
- If the memory includes a "_facts_omitted" hint, the full fact store is larger than shown. Only add facts that are genuinely new — the system handles deduplication automatically.

Return ONLY valid JSON, no explanation or markdown."""

FACT_EXTRACTION_PROMPT = """Extract factual information about the user from this message.

Message:
{message}

Extract facts in this JSON format:
{{
  "facts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ]
}}

Return ONLY valid JSON."""


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken (fallback: char/4)."""
    if not TIKTOKEN_AVAILABLE:
        return len(text) // 4
    try:
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 4


def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    """Coerce a confidence-like value to a bounded float in [0, 1]."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))
    if not math.isfinite(confidence):
        return max(0.0, min(1.0, default))
    return max(0.0, min(1.0, confidence))


def format_memory_for_injection(memory_data: dict[str, Any], max_tokens: int = 2000) -> str:
    """Format memory data for injection into system prompt — identical to the canonical format."""
    if not memory_data:
        return ""

    sections = []

    # Format user context
    user_data = memory_data.get("user", {})
    if user_data:
        user_sections = []
        work_ctx = user_data.get("workContext", {})
        if work_ctx.get("summary"):
            user_sections.append(f"Work: {work_ctx['summary']}")
        personal_ctx = user_data.get("personalContext", {})
        if personal_ctx.get("summary"):
            user_sections.append(f"Personal: {personal_ctx['summary']}")
        top_of_mind = user_data.get("topOfMind", {})
        if top_of_mind.get("summary"):
            user_sections.append(f"Current Focus: {top_of_mind['summary']}")
        avoidances = user_data.get("avoidances", {})
        if avoidances.get("summary"):
            user_sections.append(f"Avoid: {avoidances['summary']}")
        if user_sections:
            sections.append("User Context:\n" + "\n".join(f"- {s}" for s in user_sections))

    # Format history
    history_data = memory_data.get("history", {})
    if history_data:
        history_sections = []
        recent = history_data.get("recentWeeks", {})
        if recent.get("summary"):
            history_sections.append(f"Recent: {recent['summary']}")
        earlier = history_data.get("earlierContext", {})
        if earlier.get("summary"):
            history_sections.append(f"Earlier: {earlier['summary']}")
        background = history_data.get("longTermBackground", {})
        if background.get("summary"):
            history_sections.append(f"Background: {background['summary']}")
        if history_sections:
            sections.append("History:\n" + "\n".join(f"- {s}" for s in history_sections))

    # Format facts (sorted by confidence, token-budgeted)
    facts_data = memory_data.get("facts", [])
    if isinstance(facts_data, list) and facts_data:
        ranked_facts = sorted(
            (f for f in facts_data if isinstance(f, dict) and isinstance(f.get("content"), str) and f.get("content").strip()),
            key=lambda fact: _coerce_confidence(fact.get("confidence"), default=0.0),
            reverse=True,
        )
        base_text = "\n\n".join(sections)
        base_tokens = _count_tokens(base_text) if base_text else 0
        facts_header = "Facts:\n"
        separator_tokens = _count_tokens("\n\n" + facts_header) if base_text else _count_tokens(facts_header)
        running_tokens = base_tokens + separator_tokens

        fact_lines: list[str] = []
        for fact in ranked_facts:
            content = fact.get("content", "").strip()
            if not content:
                continue
            category = str(fact.get("category", "context")).strip() or "context"
            confidence = _coerce_confidence(fact.get("confidence"), default=0.0)
            source_error = fact.get("sourceError")
            if category == "correction" and isinstance(source_error, str) and source_error.strip():
                line = f"- [{category} | {confidence:.2f}] {content} (avoid: {source_error.strip()})"
            else:
                line = f"- [{category} | {confidence:.2f}] {content}"
            line_text = ("\n" + line) if fact_lines else line
            line_tokens = _count_tokens(line_text)
            if running_tokens + line_tokens <= max_tokens:
                fact_lines.append(line)
                running_tokens += line_tokens
            else:
                break

        if fact_lines:
            sections.append("Facts:\n" + "\n".join(fact_lines))

    if not sections:
        return ""

    result = "\n\n".join(sections)
    token_count = _count_tokens(result)
    if token_count > max_tokens:
        char_per_token = len(result) / token_count
        target_chars = int(max_tokens * char_per_token * 0.95)
        result = result[:target_chars] + "\n..."

    return result


def format_conversation_for_update(messages: list[Any]) -> str:
    """Format conversation messages for memory update prompt — identical to the canonical format."""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))

        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        if role == "human":
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content)).strip()
            if not content:
                continue

        if len(str(content)) > 1000:
            content = str(content)[:1000] + "..."

        if role == "human":
            lines.append(f"User: {content}")
        elif role == "ai":
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Task Memory extraction prompt
# ──────────────────────────────────────────────────────────────────────────────

TASK_MEMORY_UPDATE_PROMPT = """You are a task memory extraction system. Analyze the following completed task and extract structured knowledge that will help future agents working on similar tasks.

Task Title:
{task_title}

Task Description:
{task_description}

Task Output (result):
{task_output}

Task Status: {task_status}
Assigned Agent: {assigned_agent}

Extract the following fields as JSON. Be concise and specific. Use the same language as the task content (Chinese or English).

{{
  "summary": "1-3 sentence summary of what was accomplished and how",
  "decisions": ["Key decision 1", "Key decision 2"],
  "pitfalls": ["Mistake or problem encountered and how it was resolved"],
  "discoveries": ["Useful technique, tool quirk, or finding"],
  "tags": ["keyword1", "keyword2"]
}}

Guidelines:
- Summary: Focus on the approach and result, not just the title. Include key technologies used.
- Decisions: Record architecture choices, technology selections, design trade-offs. Max 3 entries.
- Pitfalls: Include root causes and workarounds. Omit if the task had no issues. Max 3 entries.
- Discoveries: Include effective CLI flags, API quirks, unexpected behaviors, efficient patterns. Max 3 entries.
- Tags: 3-6 short keywords for retrieval (technology names, domain terms, problem types).
- If the task failed, explain why in the summary and include the failure cause in pitfalls.
- Each string field should be concise (under 150 chars).
- Only include genuinely useful information — omit trivial details.

Return ONLY valid JSON, no explanation or markdown."""


# ──────────────────────────────────────────────────────────────────────────────
# Injection formatter
# ──────────────────────────────────────────────────────────────────────────────

def format_related_tasks_for_injection(tasks: list) -> str:
    """Format related task memories as a compact ``<task_memory>`` XML block.

    Each task entry uses ~80 tokens: short title, status icon, and at most
    2 pitfalls + 2 discoveries (truncated to 60 chars each).

    Args:
        tasks: List of ``TaskMemory`` instances.

    Returns:
        A compact XML string for injection, or empty string if *tasks* is empty.
    """
    if not tasks:
        return ""

    lines = [
        "<task_memory>",
        "Experience from similar past tasks by you or other members, for reference only:",
    ]
    for t in tasks:
        status_icon = "✅" if t.status in ("completed", "approved") else "❌"
        parts = [f"- [{t.task_id}] \"{t.task_title[:40]}\""]
        if t.assigned_agent:
            parts.append(f"→ {t.assigned_agent}")
        parts.append(f"| {status_icon}")

        # at most 2 pitfalls and 2 discoveries, each truncated to 60 chars
        details: list[str] = []
        for p in t.pitfalls[:2]:
            details.append(f"Pitfall: {p[:60]}")
        for d in t.discoveries[:2]:
            details.append(f"Discovery: {d[:60]}")
        if details:
            parts.append("| " + " | ".join(details))

        lines.append(" ".join(parts))

    lines.append("(Use memory_search(task_id=\"...\") to query full details)")
    lines.append("</task_memory>")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Team Memory extraction prompt
# ──────────────────────────────────────────────────────────────────────────────

TEAM_MEMORY_UPDATE_PROMPT = """You are a team knowledge manager. Analyze the results of a completed team run and extract team-level insights that will help future runs.

Current team memory (already known):
<current_team_memory>
{current_memory}
</current_team_memory>

Tasks completed in this run:
<completed_tasks>
{tasks_summary}
</completed_tasks>

Lead's summary of this run:
<lead_summary>
{lead_summary}
</lead_summary>

Extract NEW team-level insights as JSON. Only include genuinely new information not already in current_team_memory:

{{
  "new_practices": [
    {{
      "practice": "A team-level best practice discovered this run",
      "importance": "critical|high|medium",
      "discovered_by": "agent_name"
    }}
  ],
  "new_pitfalls": [
    {{
      "pitfall": "A cross-task problem or gotcha to watch out for",
      "affected": ["component_or_file_name"],
      "discovered_by": "agent_name"
    }}
  ],
  "run_summary": {{
    "summary": "1-2 sentence summary of what this run accomplished",
    "tasks_completed": 0,
    "tasks_failed": 0
  }}
}}

Guidelines:
- Only extract TEAM COLLABORATION lessons: who is good at what, coordination/handoff pitfalls, workflow practices. Do NOT extract individual members' domain/technical experience (API usage, coding tricks, project-specific know-how) — those are handled by a separate member-memory channel.
- Only include practices that apply across multiple tasks — not single-task tricks.
- Only include pitfalls that affected more than one task or could recur.
- Best practices should be actionable: "always X before Y" or "prefer A over B".
- If nothing new was learned this run, return empty arrays for new_practices and new_pitfalls.
- Max 3 new practices and 3 new pitfalls per run.
- Keep each practice/pitfall description under 120 chars.
- Use the same language as the task content (Chinese or English).

Return ONLY valid JSON, no explanation or markdown."""
