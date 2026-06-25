"""Prompt evaluator supporting offline datasets and A/B tests."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from harness.evaluation.judge import JudgeEvaluator
from harness.prompts.registry import PromptRegistry
from harness.prompts.storage import PromptTemplate


class PromptTestCase(BaseModel):
    """A single prompt test case."""

    id: str
    input: str
    expected_output: str | None = None
    expected_tool_calls: list[str] = []
    tags: list[str] = []
    difficulty: str = "medium"
    evaluation_criteria: list[str] = []
    context: dict[str, Any] = {}


class ABTestResult(BaseModel):
    """Result of an A/B prompt test."""

    template_a_id: str
    template_b_id: str
    score_a: float
    score_b: float
    winner: str
    sample_count: int
    created_at: datetime = datetime.now()


class PromptEvaluator:
    """Evaluate prompt templates on a dataset using a Judge."""

    def __init__(
        self,
        judge: JudgeEvaluator,
        test_dataset: list[PromptTestCase] | None = None,
    ):
        self.judge = judge
        self.test_dataset = test_dataset or []

    async def run_offline_tests(self, template: PromptTemplate) -> float:
        """Run the configured test dataset against a template and return mean score."""
        if not self.test_dataset:
            return 0.0

        scores: list[float] = []
        for case in self.test_dataset:
            prompt = await self._render_test_prompt(template, case)
            response = await self._call_llm(prompt)
            eval_result = await self.judge.evaluate_subagent_result(
                subagent_name=template.agent_type,
                instruction=case.input,
                result=response,
            )
            scores.append(eval_result.overall)

        return sum(scores) / len(scores) if scores else 0.0

    async def run_ab_test(
        self,
        template_a: PromptTemplate,
        template_b: PromptTemplate,
        samples: list[PromptTestCase] | None = None,
    ) -> ABTestResult:
        """Compare two prompt versions on the same samples."""
        samples = samples or self.test_dataset
        results_a = await self._run_batch(template_a, samples)
        results_b = await self._run_batch(template_b, samples)

        score_a = sum(r.overall for r in results_a) / len(results_a) if results_a else 0.0
        score_b = sum(r.overall for r in results_b) / len(results_b) if results_b else 0.0

        return ABTestResult(
            template_a_id=template_a.id,
            template_b_id=template_b.id,
            score_a=score_a,
            score_b=score_b,
            winner="a" if score_a >= score_b else "b",
            sample_count=len(samples or []),
        )

    async def _run_batch(
        self,
        template: PromptTemplate,
        samples: list[PromptTestCase] | None,
    ) -> list[Any]:
        from harness.models import SubAgentEvaluation

        results = []
        for case in samples or []:
            prompt = await self._render_test_prompt(template, case)
            response = await self._call_llm(prompt)
            eval_result = await self.judge.evaluate_subagent_result(
                subagent_name=template.agent_type,
                instruction=case.input,
                result=response,
            )
            results.append(eval_result)
        return results or [SubAgentEvaluation(overall=0.0)]

    async def _render_test_prompt(
        self,
        template: PromptTemplate,
        case: PromptTestCase,
    ) -> str:
        from harness.prompts.renderer import PromptRenderer

        renderer = PromptRenderer()
        variables = dict(case.context)
        variables["input"] = case.input
        return renderer.render(template.content, variables)

    async def _call_llm(self, prompt: str) -> str:
        return f"[mock llm response for prompt length {len(prompt)}]"
