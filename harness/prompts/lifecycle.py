"""Prompt template lifecycle management."""
from __future__ import annotations

from typing import Any

from harness.prompts.evaluator import PromptEvaluator
from harness.prompts.registry import PromptRegistry
from harness.prompts.storage import PromptTemplate


class PromptTransitionError(Exception):
    """Raised when a lifecycle transition precondition fails."""


class PromptLifecycle:
    """Manage template state transitions with quality gates."""

    STATES = {
        "draft": "草稿",
        "review": "人工评审",
        "testing": "离线测试",
        "staging": "灰度发布",
        "production": "全量发布",
        "deprecated": "废弃",
    }

    def __init__(self, registry: PromptRegistry):
        self.registry = registry

    async def transition(
        self,
        template_id: str,
        version: str,
        new_state: str,
        evaluator: PromptEvaluator | None = None,
    ) -> None:
        """Move a template version to ``new_state`` after checking gates."""
        if new_state not in self.STATES:
            raise PromptTransitionError(f"Unknown state: {new_state}")

        template = await self.registry.get(template_id, version)

        if new_state == "testing" and evaluator:
            score = await evaluator.run_offline_tests(template)
            if score < 0.7:
                raise PromptTransitionError(
                    f"离线评估分数 {score:.2f} 不足 0.7，无法进入测试"
                )

        if new_state == "production":
            staging_score = template.metadata.get("staging_score", 0)
            if staging_score < 0.8:
                raise PromptTransitionError(
                    f"灰度评估分数 {staging_score:.2f} 不足 0.8，无法全量发布"
                )

        await self.registry.update_state(template_id, version, new_state)
