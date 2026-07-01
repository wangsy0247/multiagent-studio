#!/usr/bin/env python3
"""最终方案验证 — 子类化 ChatOpenAI 以保留 reasoning_content。"""

import asyncio
import os
from pathlib import Path
from typing import Any, AsyncIterator

env_file = Path(__file__).resolve().parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

api_key = os.getenv("HARNESS_OPENAI_API_KEY", "")
base_url = os.getenv("HARNESS_OPENAI_BASE_URL", "")
model = os.getenv("HARNESS_DEFAULT_MODEL", "qwen3.6-plus")


# ── 子类化 ChatOpenAI，在 chunk 中保留 reasoning_content ──
class ChatOpenAIWithReasoning(ChatOpenAI):
    """ChatOpenAI 子类 — 将 reasoning_content 注入 additional_kwargs。"""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None = None,
    ) -> ChatGenerationChunk | None:
        """重写以捕获 reasoning_content。"""
        # 获取第一个 choice 的 delta
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            reasoning = delta.get("reasoning_content", "")
        else:
            reasoning = ""

        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if result and reasoning:
            # 注入 reasoning_content 到 additional_kwargs
            msg = result.message
            if hasattr(msg, "additional_kwargs"):
                msg.additional_kwargs["reasoning_content"] = reasoning
                # 确保 model_extra 也更新
                if hasattr(msg, "model_extra") and isinstance(msg.model_extra, dict):
                    msg.model_extra["reasoning_content"] = reasoning

        return result


async def main():
    print(f"model={model}\nbase_url={base_url}")

    llm = ChatOpenAIWithReasoning(
        model=model, api_key=api_key, base_url=base_url,
        temperature=0.0, extra_body={"enable_thinking": True},
    )

    events = 0
    reasoning_total = ""
    content_total = ""

    print("\n流式输出:")
    async for event in llm.astream_events(
        [HumanMessage(content="用中文解释什么是机器学习，用一句话")], version="v2"
    ):
        if event["event"] == "on_chat_model_stream":
            events += 1
            chunk = event["data"]["chunk"]

            reasoning = chunk.additional_kwargs.get("reasoning_content", "")
            content = getattr(chunk, "content", "")

            if reasoning:
                reasoning_total += reasoning
                print(f"[思考] {reasoning}", end="", flush=True)
            if content:
                content_total += str(content)
                print(f"{content}", end="", flush=True)

    print(f"\n\n统计: events={events}")
    print(f"  reasoning: {len(reasoning_total)}chars")
    print(f"  content: {len(content_total)}chars")
    if reasoning_total:
        print(f"  ✅ 成功捕获 reasoning_content!")
    else:
        print(f"  ❌ 未捕获 reasoning_content — 需要检查 _convert_chunk_to_generation_chunk 签名")


if __name__ == "__main__":
    asyncio.run(main())
