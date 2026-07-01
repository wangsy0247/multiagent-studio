"""ChatOpenAI 子类 — 保留 reasoning_content（Qwen3 / DeepSeek 思考模式）。

标准 langchain-openai 的 ChatOpenAI 在流式转换时会丢弃 delta 中的
``reasoning_content`` 字段。本模块通过子类化在 ``additional_kwargs`` 中
保留该字段，使上游（main.py 的 streaming loop 和 TitleMiddleware）可访问。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langchain_core.outputs import ChatGenerationChunk


class ChatOpenAIWithReasoning(ChatOpenAI):
    """ChatOpenAI 子类 — 在 additional_kwargs 中保留 reasoning_content。"""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None = None,
    ) -> ChatGenerationChunk | None:
        """重写以捕获 reasoning_content 并注入 additional_kwargs。"""
        # 从原始 API 响应的 delta 中提取 reasoning_content
        choices: list[dict] = chunk.get("choices", [])  # type: ignore[assignment]
        reasoning = ""
        if choices:
            delta = choices[0].get("delta", {})
            reasoning = delta.get("reasoning_content", "")

        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if result and reasoning:
            msg = result.message
            msg.additional_kwargs["reasoning_content"] = reasoning

        return result
