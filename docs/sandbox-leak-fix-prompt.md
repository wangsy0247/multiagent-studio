# 沙箱容器泄漏修复 — 修改提示词

## 任务背景

当前项目 `multiagent-studio` 的沙箱系统存在容器泄漏缺陷：默认配置下使用 `OpenSandboxProvider`（基于 Docker 容器的 OpenSandbox SDK），每次文件/Shell 工具调用都会 `OpenSandboxClient.create()` 新建一个容器，且生产代码从不调用 `release()`，容器只能靠服务端 `timeout_minutes: 30` 超时被动回收。一个 agent turn 跑 N 次工具 = 泄漏 N 个容器。

## 根因定位（代码证据）

1. **`harness/tools/sandbox_tools.py:39-46`** — `_get_sandbox()` 每次工具调用都执行 `provider.acquire()`，无任何缓存。
2. **`harness/services/open_sandbox_provider.py:256-268`** — `acquire()` 直接 `await OpenSandboxClient.create(...)` 新建容器，无按 thread_id 复用。
3. **`harness/middleware/sandbox.py:54-68`** — `SandboxMiddleware.awrap_tool_call` 仅在工具调用前 `set_sandbox_tool_context()`，工具执行后无 release 钩子；且未实现 `aafter_agent` 来释放本 turn 的沙箱。
4. **`harness/services/open_sandbox_provider.py:270-276`** — `release()` 已定义（`kill()+close()`），但全仓生产代码 0 处调用（仅 `tests/test_open_sandbox_provider.py:102` 出现一次）。

## 修复目标

将沙箱生命周期从「工具调用粒度」上移到「agent turn 粒度」：
- 每个 agent turn 只 acquire 一次，N 个工具复用同一个 sandbox 实例。
- agent turn 结束（含异常路径）时 release，确保容器被 kill+close。
- 保持 `LocalSandboxProvider` 行为不变（它本就轻量，acquire/release 是 no-op 级别）。
- 不破坏现有工具签名、不改 config.yaml schema、不引入新依赖。

## 修改清单（3 个文件）

### 文件 1：`harness/tools/sandbox_tools.py`

**改动 A — 新增一个 ContextVar 持有当前 turn 的 sandbox，并提供 acquire/release helper。**

在现有 `_tool_ctx` ContextVar（第 21-23 行）之后，新增：

```python
_current_sandbox: contextvars.ContextVar[Sandbox | None] = contextvars.ContextVar(
    "harness_current_sandbox", default=None
)


async def acquire_sandbox_for_turn() -> Sandbox:
    """Acquire (or reuse) the sandbox for the current agent turn.

    Called once by SandboxMiddleware.abefore_agent. Subsequent tool calls
    read the cached instance via _get_sandbox() without re-acquiring.
    """
    ctx = _current_ctx()
    workspace = ctx.get("workspace") or "."
    thread_id = ctx.get("thread_id") or "default"
    user_id = ctx.get("user_id")
    provider = get_sandbox_provider()
    sbx = await provider.acquire(thread_id, workspace, user_id=user_id)
    _current_sandbox.set(sbx)
    return sbx


async def release_sandbox_for_turn() -> None:
    """Release the sandbox acquired for the current agent turn.

    Called by SandboxMiddleware.aafter_agent (and on exception paths).
    Safe to call when no sandbox was acquired (no-op).
    """
    sbx = _current_sandbox.get()
    if sbx is None:
        return
    _current_sandbox.set(None)
    try:
        provider = get_sandbox_provider()
        await provider.release(sbx)
    except Exception as exc:
        logger.warning("Failed to release sandbox: %s", exc)
```

**改动 B — 修改 `_get_sandbox()`（第 39-46 行），优先复用 ContextVar 中的 sandbox，仅在缺失时回退到 acquire。**

```python
async def _get_sandbox() -> Sandbox:
    """Return the sandbox for the current turn.

    Prefers the instance cached by SandboxMiddleware.abefore_agent; falls
    back to acquiring one on-demand for backward compatibility (e.g. tools
    invoked outside an agent turn).
    """
    sbx = _current_sandbox.get()
    if sbx is not None:
        return sbx
    return await acquire_sandbox_for_turn()
```

> 说明：保留 on-demand 回退路径，避免破坏单测和直接调用工具的场景。

### 文件 2：`harness/middleware/sandbox.py`

**改动 C — `SandboxMiddleware` 实现 `abefore_agent`（acquire）和 `aafter_agent`（release），并用 try/finally 包裹工具调用。**

完整替换为：

```python
"""SandboxMiddleware — manage per-turn sandbox lifecycle.

Acquires a sandbox once at agent start, lets all tool calls in that turn
reuse it, and releases it at agent end (including exception paths).
"""
from __future__ import annotations

import logging
from typing import Any, override

from langgraph.runtime import Runtime

from harness.middleware.base import HarnessAgentMiddleware
from harness.models import HarnessState
from harness.tools.sandbox_tools import (
    acquire_sandbox_for_turn,
    release_sandbox_for_turn,
    set_sandbox_tool_context,
)

logger = logging.getLogger(__name__)


def _set_context_from_state(state: Any) -> None:
    if isinstance(state, dict):
        thread_id = state.get("thread_id", "default")
        workspace = state.get("workspace", ".")
        user_id = state.get("user_id")
    else:
        thread_id = getattr(state, "thread_id", "default")
        workspace = getattr(state, "workspace", ".")
        user_id = getattr(state, "user_id", None)
    set_sandbox_tool_context(workspace=workspace, thread_id=thread_id, user_id=user_id)


class SandboxMiddleware(HarnessAgentMiddleware):
    """Acquire/release a sandbox once per agent turn."""

    name = "sandbox"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @override
    async def abefore_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        _set_context_from_state(state)
        try:
            await acquire_sandbox_for_turn()
            logger.debug("Sandbox acquired for agent turn")
        except Exception as exc:
            # LocalSandboxProvider cannot fail here; OpenSandboxProvider
            # may fail if the server is down. Log and let tools fall back
            # to on-demand acquire (which will surface the error).
            logger.warning("Sandbox acquire failed at agent start: %s", exc)
        return None

    @override
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        state = getattr(request, "state", None)
        if state is None and isinstance(request, dict):
            state = request.get("state")
        _set_context_from_state(state)
        return await handler(request)

    @override
    async def aafter_agent(
        self,
        state: HarnessState,
        runtime: Runtime,
    ) -> dict | None:
        await release_sandbox_for_turn()
        logger.debug("Sandbox released after agent turn")
        return None
```

> 关键点：
> - `abefore_agent` acquire 一次，失败不阻塞（让工具层 on-demand 兜底）。
> - `aafter_agent` 无条件 release（release 内部已做 None 判断和异常吞咽）。
> - `awrap_tool_call` 仅刷新 context（保持原行为），不再参与 acquire/release。

### 文件 3：`harness/services/open_sandbox_provider.py`（可选但推荐）

**改动 D — 在 `OpenSandboxProvider` 中增加按 thread_id 的轻量缓存，作为双保险。**

在 `OpenSandboxProvider.__init__`（第 193-212 行）末尾追加：

```python
        self._cache: dict[str, "OpenSandbox"] = {}
        self._cache_lock = asyncio.Lock()
```

并在文件顶部 `import asyncio`。

将 `acquire`（第 256-268 行）改为：

```python
    async def acquire(
        self, thread_id: str, workspace: str, *, user_id: str | None = None
    ) -> Sandbox:
        async with self._cache_lock:
            cached = self._cache.get(thread_id)
            if cached is not None:
                return cached

        volumes = self._build_volumes(thread_id, user_id=user_id)
        sbx = await OpenSandboxClient.create(
            self.image,
            connection_config=self.connection_config,
            timeout=timedelta(minutes=self.timeout_minutes),
            entrypoint=["/bin/sh", "-c", "sleep infinity"],
            resource=self.resource,
            volumes=volumes,
        )
        wrapped = OpenSandbox(thread_id, sbx, user_id=user_id)
        async with self._cache_lock:
            # 防止并发 acquire 产生两个实例，保留先入者
            if thread_id not in self._cache:
                self._cache[thread_id] = wrapped
                return wrapped
            # 并发场景：另一个协程已创建，释放当前多余的
            concurrent = self._cache[thread_id]
        try:
            await sbx.kill()
            await sbx.close()
        except Exception:
            pass
        return concurrent
```

将 `release`（第 270-276 行）改为：

```python
    async def release(self, sandbox: Sandbox) -> None:
        if isinstance(sandbox, OpenSandbox):
            async with self._cache_lock:
                # 仅当缓存中仍是同一个实例时才清除；避免误清后续 acquire 的实例
                if self._cache.get(sandbox.thread_id) is sandbox:
                    self._cache.pop(sandbox.thread_id, None)
            try:
                await sandbox._sbx.kill()
                await sandbox._sbx.close()
            except Exception as exc:
                logger.warning("Error releasing OpenSandbox %s: %s", sandbox.thread_id, exc)
```

> 这一层缓存是双保险：即便中间件层遗漏 release，同一 thread_id 的多次 acquire 也会复用同一容器，把「N 个泄漏」降为「1 个泄漏」。中间件层正常 release 后缓存自动清除。

## 验收标准

1. **功能正确**：跑一个 agent turn，连续调用 `file_write` → `file_read` → `bash` → `grep` 4 个工具，Docker 容器数应为 1（修复前为 4）。可用 `docker ps --filter ancestor=python:3.11-slim` 在 turn 期间观察。
2. **释放生效**：agent turn 结束后，`docker ps` 中该容器应被 kill（修复前会存活 30 分钟）。
3. **异常路径**：在工具执行中抛异常，turn 结束后容器仍被释放（try/finally 或 aafter_agent 无条件 release 保证）。
4. **LocalSandbox 回归**：把 `config.yaml` 的 `sandbox.use` 改空（走 LocalSandboxProvider），所有工具行为与修复前一致。
5. **并发安全**：同一 thread_id 的两个并发 acquire 不会产生两个容器（靠 `_cache_lock` 保证）。
6. **单测**：`harness/tests/test_open_sandbox_provider.py` 全部通过；新增一个 `test_acquire_caches_per_thread` 用例验证同 thread_id 二次 acquire 返回同一实例。
7. **中间件注册顺序不变**：`harness/middleware/__init__.py` 中 `SandboxMiddleware` 仍在 `[2]` 位置，不要动注册顺序。

## 不要做的事

- 不要修改 `config.yaml` 的 schema 或默认值。
- 不要改 `Sandbox` / `SandboxProvider` 抽象基类的接口签名。
- 不要引入沙箱池/预热（over-engineering，当前问题用 per-turn 复用即可解决）。
- 不要在 `LocalSandboxProvider` 上加缓存（它本来就轻量，加了反而让两个 provider 行为不一致）。
- 不要删除 `_get_sandbox()` 中的 on-demand 回退路径（单测和直接调用工具的场景依赖它）。

## 回滚方案

若修复后出现回归，三个文件均为局部修改，可直接 git revert 对应 commit。无数据库迁移、无配置文件变更，回滚零成本。
