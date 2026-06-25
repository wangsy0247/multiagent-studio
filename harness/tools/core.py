"""Core tool factories bundled by the Harness tool registry."""
from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool



def create_ask_clarification_tool() -> Any:
    """Create the ``ask_clarification`` tool."""

    @tool
    def ask_clarification(
        question: str,
        context: str = "",
        options: list[str] | None = None,
        required: bool = False,
    ) -> str:
        """Ask the user for clarification before a sensitive action.

        Args:
            question: The clarification question.
            context: Background information.
            options: Optional list of choices.
            required: Whether an answer is required to proceed.
        """
        return f"等待用户确认: {question}"

    return ask_clarification


def create_data_query_tool() -> Any:
    """Create the ``data_query`` tool."""

    @tool
    def data_query(query: str, source: str = "") -> str:
        """Query a data source.

        Args:
            query: Query string or SQL.
            source: Data source identifier.
        """
        return f"[mock] data_query result for '{query}' from source '{source}'"

    return data_query


def create_chart_generate_tool() -> Any:
    """Create the ``chart_generate`` tool."""

    @tool
    def chart_generate(data_description: str, chart_type: str = "line") -> str:
        """Generate a chart description.

        Args:
            data_description: Description of the data to chart.
            chart_type: Chart type (line, bar, scatter, etc.).
        """
        return f"[mock] generated {chart_type} chart for: {data_description}"

    return chart_generate


def create_csv_process_tool() -> Any:
    """Create the ``csv_process`` tool."""

    @tool
    def csv_process(path: str, operation: str = "preview") -> str:
        """Process a CSV file.

        Args:
            path: Path to the CSV file.
            operation: Operation to perform.
        """
        return f"[mock] csv_process '{operation}' on {path}"

    return csv_process


def create_template_render_tool() -> Any:
    """Create the ``template_render`` tool."""

    @tool
    def template_render(template_name: str, variables: dict | None = None) -> str:
        """Render a named template.

        Args:
            template_name: Name of the template.
            variables: Variables for rendering.
        """
        return f"[mock] rendered template '{template_name}' with {variables or {}}"

    return template_render


def create_code_check_tool() -> Any:
    """Create the ``code_check`` tool."""

    @tool
    def code_check(code: str, language: str = "python") -> str:
        """Check code for simple issues.

        Args:
            code: Code to check.
            language: Programming language.
        """
        lines = code.splitlines()
        issues = []
        for i, line in enumerate(lines, 1):
            if "TODO" in line:
                issues.append(f"line {i}: contains TODO")
        return "\n".join(issues) if issues else "[ok] no obvious issues found"

    return code_check


def build_core_tools() -> list[BaseTool]:
    """Return the list of core utility tools.

    These are generic helper tools that do not belong to the search, code,
    files, abacus, or weather categories.
    """
    return [
        create_ask_clarification_tool(),
        create_data_query_tool(),
        create_chart_generate_tool(),
        create_csv_process_tool(),
        create_template_render_tool(),
        create_code_check_tool(),
    ]


# Module-level convenience instances (registry-free usage).
ask_clarification = create_ask_clarification_tool()
data_query = create_data_query_tool()
chart_generate = create_chart_generate_tool()
csv_process = create_csv_process_tool()
template_render = create_template_render_tool()
code_check = create_code_check_tool()
