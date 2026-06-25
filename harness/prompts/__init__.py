"""Harness prompts engineering package."""
from __future__ import annotations

from harness.prompts.assembler import PromptAssembler
from harness.prompts.evaluator import ABTestResult, PromptEvaluator, PromptTestCase
from harness.prompts.file_storage import FilePromptStorage
from harness.prompts.guard import PromptGuard
from harness.prompts.lifecycle import PromptLifecycle, PromptTransitionError
from harness.prompts.registry import PromptRegistry
from harness.prompts.renderer import PromptRenderer
from harness.prompts.storage import PromptStorage, PromptTemplate, PromptVersion
from harness.prompts.validator import OutputValidator


def create_prompt_registry(prompts_root: str = "./prompts") -> PromptRegistry:
    """Factory: create a registry backed by the file system."""
    return PromptRegistry(
        storage=FilePromptStorage(prompts_root),
        renderer=PromptRenderer(),
    )


__all__ = [
    "PromptStorage",
    "PromptTemplate",
    "PromptVersion",
    "FilePromptStorage",
    "PromptRenderer",
    "PromptRegistry",
    "PromptAssembler",
    "PromptEvaluator",
    "PromptTestCase",
    "ABTestResult",
    "PromptGuard",
    "OutputValidator",
    "PromptLifecycle",
    "PromptTransitionError",
    "create_prompt_registry",
]
