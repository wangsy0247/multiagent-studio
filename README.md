# MultiAgent Studio

一个多智能体（Multi-Agent）开发与协作平台：在 Web 界面上创建、编排和观察 AI Agent —— 既可以用**单 Agent 模式**对话式完成任务，也可以用 **Team 模式**让 Lead Agent 拆解目标、委派给多个专业成员并行协作，并对产出进行审查验收。

## 功能特性

- **单 Agent 模式** — 流式对话、斜杠指令（`/compact`、`/clear`）、@提及切换 Agent、文件上传与产物预览
- **Team 模式** — Lead Agent 自动拆解目标、按成员能力边界委派任务、计划审批（plan approval）、独立 Verifier 验收、团队记忆沉淀
- **自定义 Agent** — 每个 Agent 由 `SOUL.md` 人格定义 + 配置（温度 / 最大 Token / 运行限制）组成，支持长期记忆
- **SubAgent 委派** — 预置 researcher / coder / analyst / writer / reviewer，可按需 spawn 子 Agent 并行执行
- **MCP 工具集成** — 兼容 MCP 协议接入外部工具（GitHub、搜索、文件系统等），内置 tool_search 延迟加载，工具数量多时自动按需检索 schema
- **技能系统（Skills）** — 内置技能 + 用户自定义技能，支持运行中自进化沉淀新技能
- **记忆系统** — 用户记忆 / 项目记忆 / 任务记忆 / 团队记忆分层管理，自动提取事实并在对话时注入
- **沙箱代码执行** — 基于 OpenSandbox 的隔离执行环境（可选，需 Docker）
- **定时任务** — Agent 可创建 cron 定时任务，由内置调度器执行
- **可观测性** — Langfuse 集成 + 本地 trace 文件，Token 用量统计

## 架构

```
┌────────────────┐   ┌────────────────┐   ┌─────────────────────┐
│  Frontend      │   │  App (业务层)   │   │  Harness (运行时)    │
│  Next.js :3000 │──▶│  FastAPI :8000 │──▶│  FastAPI :8001      │
│  聊天/监控/设置 │   │  鉴权/会话/文件 │   │  LangGraph Agent    │
└────────────────┘   └────────────────┘   │  Team 编排 / 工具    │
                                          │  记忆 / 沙箱 / MCP   │
                                          └─────────────────────┘
```

- **Frontend** — Next.js 14 + Tailwind + Zustand，SSE 流式渲染执行过程
- **App** — 用户/会话/项目/文件/定时任务管理，JWT 鉴权，SQLite（可换 PostgreSQL）
- **Harness** — Agent 运行时：LangGraph 状态图 + 中间件链（动态上下文、摘要压缩、循环检测、工具过滤）+ Team 编排器

## 快速开始

### 前置条件

- [Miniconda](https://docs.conda.io/en/latest/miniconda.html)（管理 Python 环境）
- Node.js ≥ 18.18（推荐 LTS）
- Docker（可选 — 仅沙箱代码执行需要）

### 安装并启动

```bash
git clone <repo-url> && cd multiagent-studio

make setup   # 首次安装: 创建 conda 环境、生成配置、安装依赖 (幂等)
make run     # 启动全部服务
```

`make setup` 会自动完成：

1. 创建 conda 环境 `harness`（Python 3.12）并安装后端依赖
2. 生成根 `.env`（自动填入随机 `JWT_SECRET` / `INTERNAL_API_TOKEN`）
3. 生成 `harness/.env`，**交互式询问模型 API 配置**
4. 安装前端依赖

`make run` 依次启动三个服务：

| 服务 | 端口 | 说明 |
|------|------|------|
| Harness | 8001 | Agent 运行时（内部服务，无需对外） |
| App | 8000 | 业务层 API（文档: `:8000/docs`） |
| Frontend | 3000 | Web 界面入口 |

打开 `http://localhost:3000` 注册账号即可使用 —— 模型 API 由服务器统一配置，**用户无需填写任何 API Key**。

其他命令：`make stop`（停止全部服务）、`make help`。

### 模型配置（服务器管理员）

模型由服务器统一管理，编辑 `harness/.env`：

```bash
OPENAI_API_KEY=sk-...                                  # 必填
OPENAI_BASE_URL=https://api.openai.com/v1              # 任意 OpenAI 兼容端点 (通义千问/DeepSeek 等均可)
DEFAULT_MODEL=gpt-4o                                   # 主模型
# 可选: 辅助任务使用更便宜的模型 (留空 = 回退主模型)
SUMMARY_MODEL=     # 长上下文摘要压缩
TITLE_MODEL=       # 会话标题生成
MEMORY_MODEL=      # 记忆事实提取
```

修改后重启服务生效。用户 YAML 中即使存在旧 key 也会被服务器配置强制覆盖。

### 可选集成（harness/.env）

| 变量 | 作用 |
|------|------|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 链路追踪 |
| `TAVILY_API_KEY` / `SERPAPI_API_KEY` | 网络搜索工具 |
| `GITHUB_TOKEN` | GitHub MCP Server |
| `SANDBOX_IMAGE` | 沙箱镜像（默认 `python:3.12`，可换镜像源） |

## 项目结构

```
├── app/            # 业务层: 鉴权 / 会话 / 项目 / 文件 / 定时任务
├── harness/        # Agent 运行时: LangGraph / Team 编排 / 工具 / 记忆 / 沙箱 / MCP
│   ├── agents/     # LeadAgent / 预设 SubAgent
│   ├── team/       # Team 模式: 编排器 / 成员 Agent / 任务存储 / 消息总线
│   ├── middleware/ # 中间件链: 动态上下文 / 摘要 / 循环检测 / 标题
│   ├── memory/     # 记忆提取与存储
│   └── config/     # 三层配置合并 (L0 系统默认 → L1 用户 → L2 Agent)
├── frontend/       # Next.js 前端
├── skills/builtin/ # 内置技能
├── scripts/        # 数据迁移脚本
├── nginx/          # 生产环境反代配置 (可选)
└── setup.sh / start.sh / Makefile
```

## 测试

```bash
conda run -n harness python -m pytest harness/tests -q   # Agent 运行时
conda run -n harness python -m pytest app/tests -q       # 业务层
cd frontend && npx tsc --noEmit                          # 前端类型检查
```

## 开源协议

本项目以 [MIT License](LICENSE) 开源。

依赖的第三方软件包均为宽松开源协议（MIT / BSD / Apache-2.0 / ISC），包括 LangChain、LangGraph、FastAPI、Next.js、React、OpenSandbox 等，与 MIT 协议兼容。
