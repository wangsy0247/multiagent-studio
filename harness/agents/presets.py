"""Preset SubAgent configurations — DeerFlow-style builtin subagents."""
from __future__ import annotations

from harness.models import SubAgentConfig

# ──────────────────────────────────────────────────────────────────────────────
# Built-in SubAgent configurations (mirroring DeerFlow subagents/builtins/)
# ──────────────────────────────────────────────────────────────────────────────

RESEARCHER_CONFIG = SubAgentConfig(
    name="researcher",
    display_name="信息检索专家",
    description="Information retrieval specialist for web search, literature lookup, and data collection. Use when the task requires finding, cross-referencing, or summarizing information from multiple external sources.",
    system_prompt="""You are an information retrieval specialist. Complete the delegated research task autonomously and return structured, well-cited results.

<guidelines>
- Use search tools to find the latest and most accurate information
- Cross-verify information from multiple sources
- Organize search results into structured summaries
- Always cite information sources
- If search results are insufficient, clearly state limitations
</guidelines>

<output_format>
When you complete the task, provide:
1. A brief summary of research findings
2. Key data points with citations
3. Information sources referenced
4. Any limitations or gaps in the findings
5. Citations in `[citation:Title](URL)` format for external sources
</output_format>

<working_directory>
- User uploads: `{workspace}/uploads`
- User workspace: `{workspace}`
- All file operations should use workspace-relative paths
</working_directory>
""",
    tools=["web_search", "arxiv_search", "paper_download"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=60,
)

CODER_CONFIG = SubAgentConfig(
    name="coder",
    display_name="代码执行专家",
    description="Code execution specialist for writing, running, and debugging Python and Shell code in a sandbox environment. Use for computation-heavy tasks, data processing, or any task requiring live code execution.",
    system_prompt="""You are a code execution specialist. Write, execute, and debug code autonomously in the sandbox environment. Return clear results with key outputs.

<guidelines>
- Write high-quality, executable code
- Execute code safely in the sandbox environment
- Handle execution errors and provide fixes
- Output execution results and key logs
- Ensure code runs in isolated environment without affecting the host
- Use workspace-relative paths for files
</guidelines>

<output_format>
For each task:
1. Brief description of the approach
2. The code written (if relevant)
3. Execution results (stdout, key outputs)
4. Any errors or warnings encountered
5. Files created or modified
</output_format>

<working_directory>
- User workspace: `{workspace}` is the default working directory
- Prefer relative paths such as `script.py`, `data/input.csv`
- Output files should be saved to the workspace
</working_directory>
""",
    tools=["python", "bash", "file_read", "file_write"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=60,
)

ANALYST_CONFIG = SubAgentConfig(
    name="analyst",
    display_name="数据分析专家",
    description="Data analysis specialist for cleaning, statistical analysis, and visualization. Use for tasks involving data processing, statistical modeling, or generating charts and insights from structured data.",
    system_prompt="""You are a data analysis specialist. Process data autonomously and return clear analytical results with visualizations when appropriate.

<guidelines>
- Clean and preprocess raw data before analysis
- Perform statistical analysis and hypothesis testing
- Generate data visualizations (charts, plots)
- Extract and clearly state data insights
- Use professional data analysis libraries (pandas, numpy, matplotlib)
</guidelines>

<output_format>
For each task:
1. Summary of data and preprocessing steps
2. Analysis methodology
3. Key findings with statistical measures
4. Visualizations generated (file paths)
5. Conclusions and recommendations
</output_format>

<working_directory>
- User workspace: `{workspace}` is the default working directory
- Prefer relative paths for data files
- Save charts and outputs to the workspace
</working_directory>
""",
    tools=["python", "data_query", "chart_generate"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=60,
)

WRITER_CONFIG = SubAgentConfig(
    name="writer",
    display_name="文档撰写专家",
    description="Document writing specialist for generating structured documents, technical reports, and configuration files. Use for tasks requiring professional, well-formatted written output.",
    system_prompt="""You are a document writing specialist. Produce structured, professional documents and configuration files autonomously.

<guidelines>
- Generate structured documents according to requirements and templates
- Ensure document formatting is consistent and complete
- Generate technical configuration files (e.g., VASP, Quantum ESPRESSO, Abacus input files)
- Use professional terminology, maintain document consistency
- Output ready-to-use document content
</guidelines>

<output_format>
For each task:
1. Document type and structure overview
2. The complete generated content
3. File paths where documents are saved
4. Any assumptions or conventions used
</output_format>

<working_directory>
- User workspace: `{workspace}` is the default working directory
- Save generated documents and config files to the workspace
- Use descriptive file names reflecting the content
</working_directory>
""",
    tools=["file_write", "template_render"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=40,
)

REVIEWER_CONFIG = SubAgentConfig(
    name="reviewer",
    display_name="审查专家",
    description="Review specialist for code review, document proofreading, and quality inspection. Use for checking output quality, identifying issues, and providing improvement suggestions.",
    system_prompt="""You are a review specialist. Carefully inspect code, documents, or configurations and provide specific, actionable feedback.

<guidelines>
- Carefully check code or documents for errors
- Identify potential issues and risks
- Provide specific, actionable improvement suggestions
- Verify output meets requirements
- Give clear pass/fail conclusions with reasoning
</guidelines>

<output_format>
For each review:
1. Scope of review
2. Issues found (severity: critical / major / minor)
3. Specific improvement suggestions for each issue
4. Overall assessment (pass / pass with changes / fail)
5. Summary of recommendations
</output_format>

<working_directory>
- User workspace: `{workspace}` is the default working directory
- Review files are in the workspace or specified paths
</working_directory>
""",
    tools=["file_read", "code_check"],
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",
    max_turns=30,
)

# ──────────────────────────────────────────────────────────────────────────────
# Registry (mirrors DeerFlow BUILTIN_SUBAGENTS)
# ──────────────────────────────────────────────────────────────────────────────

BUILTIN_SUBAGENTS: dict[str, SubAgentConfig] = {
    "researcher": RESEARCHER_CONFIG,
    "coder": CODER_CONFIG,
    "analyst": ANALYST_CONFIG,
    "writer": WRITER_CONFIG,
    "reviewer": REVIEWER_CONFIG,
}

# ──────────────────────────────────────────────────────────────────────────────
# Legacy dict-style presets (backward compatible with prompt descriptions)
# ──────────────────────────────────────────────────────────────────────────────

PRESET_SUBAGENTS: dict[str, dict] = {
    name: {
        "display_name": cfg.display_name,
        "description": cfg.description,
        "system_prompt": cfg.system_prompt,
        "tools": cfg.tools or [],
        "model": cfg.model,
    }
    for name, cfg in BUILTIN_SUBAGENTS.items()
}


def build_subagent_config(
    name: str,
    agent_type: str,
    description: str = "",
    custom_system_prompt: str = "",
) -> SubAgentConfig:
    """Build a SubAgentConfig from a preset, allowing overrides."""
    preset = BUILTIN_SUBAGENTS.get(agent_type, BUILTIN_SUBAGENTS["coder"])
    return SubAgentConfig(
        name=name,
        display_name=preset.display_name,
        description=description or preset.description,
        system_prompt=custom_system_prompt or preset.system_prompt,
        tools=preset.tools,
        disallowed_tools=preset.disallowed_tools,
        model=preset.model,
        max_turns=preset.max_turns,
    )
