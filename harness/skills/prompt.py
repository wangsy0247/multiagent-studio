"""Skill system prompt generation — builds the ``<skill_system>`` XML block.

Kept separate from ``lead_agent.py`` to avoid importing the full middleware
chain when only the skill prompt section is needed.
"""


def _skill_label(skill) -> str:
    """Human-readable ownership label for a skill."""
    return "mine" if skill.user_id else "built-in"


def get_skills_prompt_section(
    skills: list,
    container_base_path: str = "/mnt/skills",
) -> str:
    """Generate the ``<skill_system>`` XML block listing available skills.

    Uses DeerFlow's progressive-loading pattern:
    1. LLM sees skill names, descriptions, and file locations in the prompt.
    2. When a query matches a skill, LLM calls ``file_read`` on the skill's
       ``<location>`` to load the full SKILL.md.
    3. LLM follows the skill's instructions and loads referenced resources
       on demand.

    Returns an empty string when *skills* is empty.
    """
    if not skills:
        return ""

    skill_items = "\n".join(
        f"""    <skill>
        <name>{s.name}</name>
        <description>{s.description} [{_skill_label(s)}]</description>
        <location>{s.get_container_file_path(container_base_path)}</location>
    </skill>"""
        for s in skills
    )

    return f"""<skill_system>
You have access to skills that provide optimized workflows for specific tasks.
Each skill contains best practices, frameworks, and references to additional resources.

**Progressive Loading Pattern:**
1. When a user query matches a skill's use case, immediately call `file_read` (or the
   equivalent read tool) on the skill's main file using the <location> provided below
2. Read and understand the skill's workflow and instructions
3. The skill file may reference additional resources (scripts, references, templates)
   under the same directory — load them only when needed during execution
4. Follow the skill's instructions precisely

**Skills Root:** {container_base_path}

<available_skills>
{skill_items}
</available_skills>
</skill_system>"""
