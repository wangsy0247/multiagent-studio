# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

```bash
# Setup (first time, idempotent): creates conda env, generates configs, installs deps
make setup

# Start all three services (Harness :8001, App :8000, Frontend :3000)
make run

# Stop all services
make stop

# Python tests
conda run -n harness python -m pytest harness/tests -q
conda run -n harness python -m pytest app/tests -q

# Python lint (ruff)
conda run -n harness ruff check harness/ app/

# Frontend type-check
cd frontend && npx tsc --noEmit

# Frontend lint
cd frontend && npm run lint

# Frontend dev server (standalone)
cd frontend && npm run dev

# Harness standalone (requires being in project root)
conda run -n harness python -m harness.main

# App standalone
conda run -n harness uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Architecture

### Three-service split

| Service | Port | Stack | Role |
|---------|------|-------|------|
| **Harness** | 8001 | FastAPI + LangGraph | Agent runtime: ReAct loop, Team orchestration, tools, memory, MCP, sandbox |
| **App** | 8000 | FastAPI + SQLAlchemy + SQLite | Business layer: auth (JWT), sessions, files, configs, cron scheduler, proxies Harness SSE |
| **Frontend** | 3000 | Next.js 14 + Tailwind + Zustand | Chat UI, agent/project management, monitoring dashboards |

### Harness — Agent runtime core

The agent execution graph lives in `harness/graph_factory.py`:
- **Inner graph**: `create_agent()` (from `langchain.agents`) handles the model ↔ tools ReAct loop, with all middleware as `AgentMiddleware` instances
- **Outer graph**: `StateGraph(HarnessState)` — single node `"agent"` → `END`. No separate memory-update node; memory is entirely middleware-driven

**Startup flow** (`harness/main.py` → `HarnessService.initialize()`):
1. Load bootstrap `EffectiveConfig` to read infrastructure fields
2. Load tools from `harness/config.yaml` into `ToolRegistry`, then MCP tools
3. Initialize Langfuse observability, checkpointer (SQLite by default), sandbox provider, memory storage/queue, skill registry
4. Graph compilation is **lazy** — deferred to first `execute()` call per `(user_id, agent_name)`, then cached with config-signature invalidation

**Middleware chain** (`harness/middleware/`): extends `HarnessAgentMiddleware` (which extends LangChain `AgentMiddleware[HarnessState]`). Each middleware overrides only the hooks it needs — `abefore_agent`, `aafter_agent`, `abefore_model`, `aafter_model`, `awrap_model_call`, `awrap_tool_call`. Key middleware:
- `dynamic_context.py` — injects memory/skills/task context before each agent turn
- `summarization.py` — triggers conversation summarization when token threshold exceeded
- `loop_detection.py` — detects and breaks ReAct loops
- `deferred_tool_filter.py` — enforces per-subagent tool allow/deny lists
- `clarification.py` — pauses execution for human clarification
- `subagent_limit.py` — enforces max concurrent subagents
- `token_usage.py` — tracks per-turn token consumption

**Config system** (`harness/config/`): three-layer merge — L0 system defaults (`defaults.py`) → L1 user global (`users/{uid}/config.yaml`) → L2 per-agent (`users/{uid}/agents/{name}/config.yaml`). Server-enforced keys (`model`, `api_key`, `base_url`) are always taken from `harness/.env`, overriding user/agent YAML. Supports `${VAR}` and `${VAR:-default}` env var interpolation.

**Team mode** (`harness/team/`): `Orchestrator` decomposes goals, delegates to `TeammateAgent` instances via `MessageBus`, with `TaskStore` for tracking and `VerifierAgent` for review. Uses `TeamContext` for shared memory across members.

**Tools** (`harness/tools/`): `ToolRegistry` loads tools from config, MCP servers, and builtins. `tool_search` provides lazy schema retrieval when many tools are available.

**Memory** (`harness/memory/`): layered — user memory, project memory, task memory, team memory. Facts extracted via LLM calls (debounced via `MemoryQueue`), stored in `FileMemoryStorage`, injected by `DynamicContextMiddleware`.

### App — Business layer

FastAPI app (`app/main.py`) with routers: auth, threads, execute (proxies to Harness), files, configs, monitoring, agents, projects, scheduled_tasks, internal, extensions. Uses SQLAlchemy with Alembic migrations (`app/db/`). JWT auth with refresh tokens. The scheduler (`app/services/scheduler.py`) runs user-created cron tasks.

### Frontend

Next.js 14 App Router (`frontend/src/app/`). Dashboard layout (`(dashboard)/layout.tsx`) wraps the main views: chat threads, agent management, projects, monitoring, settings, admin. State management via Zustand stores in `frontend/src/lib/`:
- `chat-store.ts` — messages, streaming, input, mode (single/team/plan)
- `auth-store.ts` — JWT tokens, user session
- `team-store.ts` — Team mode state (members, tasks, plan approval)
- `sse-client.ts` — SSE connection management for streaming responses

SSE streaming carries typed events (text chunks, tool calls, subagent spawn/completion, process steps) rendered by `MessageItem` → `ProcessGroup` components.

### Key data directories (not in git)

- `data/` — SQLite databases, user configs, memory storage, run journals
- `harness/.env` — model API keys (server-managed)
- `harness/config.yaml` — infrastructure config (sandbox, memory, checkpointer backends)
