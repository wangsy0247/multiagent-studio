"""Observability manager backed by Langfuse 4.x."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from harness.config import HarnessConfig
from harness.models import SubAgentResult, TokenUsage

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
    _LANGFUSE_AVAILABLE = True
except Exception:
    _LANGFUSE_AVAILABLE = False
    Langfuse = Any


class ObservabilityManager:
    """Trace execution, log generations and token usage via Langfuse 4.x.

    Langfuse 4.x uses ``start_observation(as_type=...)`` instead of the old
    ``trace()`` / ``span()`` / ``generation()`` methods.
    """

    def __init__(self, config: HarnessConfig | dict[str, Any]):
        self.enabled = False
        self.langfuse: Any | None = None
        # 存储活跃的 observations，以便后续 end()
        self._observations: dict[str, Any] = {}
        # 内存中累积 token 使用记录（跨线程聚合）
        self._token_records: list[dict[str, Any]] = []

        cfg = config if isinstance(config, dict) else config.model_dump()

        if not cfg.get("langfuse_enabled") or not _LANGFUSE_AVAILABLE:
            return

        public_key = cfg.get("langfuse_public_key", "")
        secret_key = cfg.get("langfuse_secret_key", "")
        host = cfg.get("langfuse_host", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            logger.info("Langfuse disabled: missing public/secret key")
            return

        try:
            self.langfuse = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
            )
            self.enabled = True
            logger.info("Langfuse initialized (host=%s)", host)
        except Exception as exc:
            logger.warning("Failed to initialize Langfuse: %s", exc)

    def start_trace(
        self,
        thread_id: str,
        user_id: str,
        name: str = "harness_execution",
    ) -> str:
        """开始一个顶层 trace（以 span 形式），返回 trace_id。"""
        if not self.enabled or self.langfuse is None:
            return thread_id

        try:
            obs = self.langfuse.start_observation(
                name=name,
                as_type="span",
                input={"thread_id": thread_id, "user_id": user_id},
                metadata={
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "start_time": datetime.now().isoformat(),
                },
            )
            self._observations[thread_id] = obs
            trace_id = getattr(obs, "trace_id", thread_id)
            logger.debug("Trace started: thread=%s trace_id=%s", thread_id, trace_id)
            return trace_id
        except Exception as exc:
            logger.warning("Failed to start trace: %s", exc)
            return thread_id

    def create_span(
        self,
        trace_id: str,
        name: str,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """在 trace 下创建子 span。"""
        if not self.enabled or self.langfuse is None:
            return None

        try:
            obs = self.langfuse.start_observation(
                trace_context={"trace_id": trace_id},
                name=name,
                as_type="span",
                metadata=metadata or {},
            )
            return obs
        except Exception as exc:
            logger.warning("Failed to create span: %s", exc)
            return None

    def create_generation(
        self,
        trace_id: str,
        name: str,
        model: str,
        messages: list[Any],
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """创建 generation observation。"""
        if not self.enabled or self.langfuse is None:
            return None

        try:
            obs = self.langfuse.start_observation(
                trace_context={"trace_id": trace_id},
                name=name,
                as_type="generation",
                model=model,
                input=messages,
                metadata=metadata or {},
            )
            return obs
        except Exception as exc:
            logger.warning("Failed to create generation: %s", exc)
            return None

    def log_token_usage(
        self,
        trace_id: str,
        generation_id: str,
        usage: TokenUsage,
    ) -> None:
        """记录 token 消耗到内存 + Langfuse。"""
        # 存入内存（即时可查询）
        record = {
            "trace_id": trace_id,
            "timestamp": datetime.now().isoformat(),
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
        }
        self._token_records.append(record)

        # 保持最多 10000 条记录，超出淘汰最旧一半
        if len(self._token_records) > 10000:
            self._token_records = self._token_records[5000:]

        # 同步记录到 Langfuse
        if not self.enabled or self.langfuse is None:
            return

        try:
            self.langfuse.start_observation(
                trace_context={"trace_id": trace_id},
                name="token_usage",
                as_type="generation",
                input={
                    "prompt_tokens": usage.prompt_tokens,
                    "total_tokens": usage.total_tokens,
                },
                output={
                    "completion_tokens": usage.completion_tokens,
                    "cost_usd": usage.cost_usd,
                },
                usage_details={
                    "input": usage.prompt_tokens,
                    "output": usage.completion_tokens,
                    "total": usage.total_tokens,
                },
                cost_details={
                    "input": usage.cost_usd / 2 if usage.cost_usd else 0,
                    "output": usage.cost_usd / 2 if usage.cost_usd else 0,
                    "total": usage.cost_usd,
                },
            ).end()
        except Exception as exc:
            logger.debug("Failed to log token usage to Langfuse: %s", exc)

    def log_tool_call(
        self,
        trace_id: str,
        tool_name: str,
        input_args: dict[str, Any],
        output: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        """记录工具调用。"""
        if not self.enabled or self.langfuse is None:
            return

        try:
            status = "error" if error else "success"
            obs = self.langfuse.start_observation(
                trace_context={"trace_id": trace_id},
                name=f"tool:{tool_name}",
                as_type="tool",
                input=input_args,
                metadata={"duration_ms": duration_ms, "status": status},
            )
            obs.end(output={"error": error} if error else output)
        except Exception as exc:
            logger.debug("Failed to log tool call: %s", exc)

    def log_subagent_execution(
        self,
        trace_id: str,
        subagent_name: str,
        instruction: str,
        result: SubAgentResult,
        duration_ms: int,
    ) -> None:
        """记录 SubAgent 执行。"""
        if not self.enabled or self.langfuse is None:
            return

        try:
            obs = self.langfuse.start_observation(
                trace_context={"trace_id": trace_id},
                name=f"subagent:{subagent_name}",
                as_type="agent",
                input={"instruction": instruction},
                metadata={
                    "duration_ms": duration_ms,
                    "status": result.status,
                    "iterations": result.iterations,
                },
            )
            obs.end(output=result.model_dump())
        except Exception as exc:
            logger.debug("Failed to log subagent execution: %s", exc)

    def finalize_trace(self, trace_id: str, status: str = "success") -> None:
        """结束 trace — 对 top-level observation 调用 end()。"""
        if not self.enabled or self.langfuse is None:
            return

        try:
            # 查找并结束与 trace_id 关联的 observation
            for tid, obs in list(self._observations.items()):
                if getattr(obs, "trace_id", "") == trace_id:
                    obs.end(
                        output={"status": status},
                        metadata={
                            "end_time": datetime.now().isoformat(),
                            "status": status,
                        },
                    )
                    del self._observations[tid]
                    logger.debug("Trace finalized: %s status=%s", trace_id, status)
                    break
            # 确保数据 flush
            self.langfuse.flush()
        except Exception as exc:
            logger.debug("Failed to finalize trace: %s", exc)

    def get_trace(self, thread_id: str) -> dict[str, Any]:
        """返回 trace 元数据。"""
        return {
            "trace_id": thread_id,
            "enabled": self.enabled,
            "status": "unknown",
        }

    def get_token_usage(
        self,
        user_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """返回 token 使用统计（从内存记录聚合）。"""
        records = self._token_records

        # 时间范围过滤
        if start_date or end_date:
            filtered: list[dict[str, Any]] = []
            for r in records:
                ts = r.get("timestamp", "")
                if start_date and ts < start_date:
                    continue
                if end_date and ts > end_date:
                    continue
                filtered.append(r)
            records = filtered

        total_prompt = sum(r.get("prompt_tokens", 0) for r in records)
        total_completion = sum(r.get("completion_tokens", 0) for r in records)
        total_all = sum(r.get("total_tokens", 0) for r in records)
        total_cost = sum(r.get("cost_usd", 0) for r in records)

        # 按模型聚合
        by_model: dict[str, dict[str, int | float]] = {}
        for r in records:
            model = r.get("model", "unknown")
            if model not in by_model:
                by_model[model] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
            by_model[model]["prompt_tokens"] += r.get("prompt_tokens", 0)
            by_model[model]["completion_tokens"] += r.get("completion_tokens", 0)
            by_model[model]["total_tokens"] += r.get("total_tokens", 0)
            by_model[model]["cost_usd"] += r.get("cost_usd", 0.0)

        # 按日期聚合
        by_date_map: dict[str, dict[str, int | float]] = {}
        for r in records:
            date_str = r.get("timestamp", "")[:10]  # YYYY-MM-DD
            if not date_str:
                continue
            if date_str not in by_date_map:
                by_date_map[date_str] = {"tokens": 0, "cost": 0.0}
            by_date_map[date_str]["tokens"] += r.get("total_tokens", 0)
            by_date_map[date_str]["cost"] += r.get("cost_usd", 0.0)

        by_date = [
            {"date": d, "tokens": v["tokens"], "cost": v["cost"]}
            for d, v in sorted(by_date_map.items())
        ]

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_all,
            "total_cost_usd": total_cost,
            "by_model": by_model,
            "by_date": by_date,
        }
