"""Prompt templates for memory update and injection — adapted from DeerFlow."""

import math
import re
from typing import Any

try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

# ── Memory update prompt (identical to DeerFlow) ──────────────────────────
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

**History** (Temporal context - rich paragraphs):
- recentWeeks: Detailed summary of activities in the past few weeks (4-6 sentences or 1-2 paragraphs)
- earlierContext: Important historical patterns (3-5 sentences or 1 paragraph)
- longTermBackground: Persistent background and foundational context (2-4 sentences)

**Facts Extraction**:
- Extract specific, quantifiable details
- Include proper nouns (company names, project names, technology names)
- CRITICAL: detect the user's preferred language — if the user consistently writes in Chinese, record as fact: "User prefers communicating in Chinese" with category "preference" and confidence 0.95+
- Categories: preference, knowledge, context, behavior, goal, correction
- Confidence levels: 0.9-1.0 explicit, 0.7-0.8 strongly implied, 0.5-0.6 inferred patterns

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
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
- Follow length guidelines: workContext/personalContext are concise, topOfMind and history are detailed
- Only add facts that are clearly stated (0.9+) or strongly implied (0.7+)
- Use category "correction" for explicit agent mistakes; assign confidence >= 0.95
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
    """Format memory data for injection into system prompt — identical to DeerFlow."""
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
    """Format conversation messages for memory update prompt — identical to DeerFlow."""
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
