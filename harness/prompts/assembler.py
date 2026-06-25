"""Dynamic prompt assembler using fragments and templates."""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from harness.prompts.registry import PromptRegistry


class PromptAssembler:
    """Assemble full prompt message lists from templates and fragments."""

    def __init__(self, registry: PromptRegistry):
        self.registry = registry

    async def assemble(
        self,
        agent_type: str,
        base_template_id: str,
        context: dict[str, Any],
    ) -> list[SystemMessage | HumanMessage]:
        """Build messages: system prompt + optional fragments + user input."""
        messages: list[SystemMessage | HumanMessage] = []

        system_prompt = await self.registry.render(
            base_template_id,
            {
                "role": context.get("role"),
                "capabilities": context.get("capabilities", []),
                "constraints": context.get("constraints", []),
                "subagent_types": context.get("subagent_types", []),
                "max_concurrent": context.get("max_concurrent", 3),
            },
        )
        messages.append(SystemMessage(content=system_prompt))

        memory_context = context.get("memory_context")
        if memory_context:
            memory_fragment = await self._render_fragment(
                "fragments:memory_context",
                {"memory": memory_context},
            )
            if memory_fragment:
                messages.append(SystemMessage(content=memory_fragment))

        tools = context.get("tools")
        if tools:
            tools_fragment = await self._build_tools_fragment(tools)
            if tools_fragment:
                messages.append(SystemMessage(content=tools_fragment))

        examples = context.get("examples")
        if examples:
            examples_fragment = await self._build_examples_fragment(
                examples,
                context.get("query"),
            )
            if examples_fragment:
                messages.append(SystemMessage(content=examples_fragment))

        messages.append(HumanMessage(content=context["input"]))
        return messages

    async def _render_fragment(
        self,
        fragment_id: str,
        variables: dict[str, Any],
    ) -> str:
        try:
            return await self.registry.render(fragment_id, variables)
        except (FileNotFoundError, ValueError, KeyError):
            return ""

    async def _build_tools_fragment(self, tools: list[BaseTool]) -> str:
        descriptions = []
        for tool in tools:
            args = ""
            if tool.args_schema:
                try:
                    args = str(tool.args_schema.model_json_schema())
                except Exception:
                    args = str(tool.args)
            descriptions.append(
                f"- `{tool.name}`: {tool.description}\n  参数: {args}"
            )
        return await self._render_fragment(
            "fragments:tools_instruction",
            {"tools": "\n".join(descriptions)},
        )

    async def _build_examples_fragment(
        self,
        examples: list[dict[str, Any]],
        query: str | None,
    ) -> str:
        selected = await self._select_examples(query, examples, top_k=3)
        return await self._render_fragment(
            "fragments:few_shot",
            {"examples": selected},
        )

    async def _select_examples(
        self,
        query: str | None,
        examples: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not query:
            return examples[:top_k]
        query_words = set(query.lower().split())
        scored = []
        for ex in examples:
            text = str(ex.get("input", "")).lower()
            score = len(query_words & set(text.split()))
            scored.append((score, ex))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ex for _, ex in scored[:top_k]]
