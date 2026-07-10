# Git Worktree SubAgent 隔离 — 完整技术分析

## 目录

1. [概述](#概述)
2. [三层调用关系](#三层调用关系)
3. [第 0 层：系统启动](#第-0-层系统启动)
4. [第 1 层：仓库初始化 — ensure_git_repo()](#第-1-层仓库初始化--ensure_git_repo)
5. [第 2 层：Worktree 创建 — create()](#第-2-层worktree-创建--create)
6. [第 3 层：上下文注入 — _build_initial_state()](#第-3-层上下文注入--_build_initial_state)
7. [第 4 层：变更合并 — merge()](#第-4-层变更合并--merge)
8. [第 5 层：清理销毁 — cleanup()](#第-5-层清理销毁--cleanup)
9. [第 6 层：残留清理 — cleanup_stale()](#第-6-层残留清理--cleanup_stale)
10. [并发模型与时序](#并发模型与时序)
11. [完整生命周期图](#完整生命周期图)
12. [技术知识点汇总](#技术知识点汇总)

---

## 概述

当主 Agent 派发并行 SubAgent 任务时，所有 SubAgent 共享同一个沙箱 workspace。Git Worktree 机制为每个 SubAgent 在 `.worktrees/` 下创建独立的工作目录，利用 Git 原生的多工作目录能力实现文件级隔离。SubAgent 完成后，通过 `git merge` 将变更合并回主 workspace，然后清理 worktree。

### 核心文件

| 文件 | 职责 |
|------|------|
| `harness/worktree/types.py` | 数据模型：`WorktreeContext`、`MergeResult`、`WorktreeConfig` |
| `harness/worktree/manager.py` | 核心管理器：`GitWorktreeManager` — 完整生命周期 |
| `harness/agents/subagent_manager.py` | 编排层：`execute()` 包裹 worktree 生命周期 |
| `harness/agents/subagent_executor.py` | 执行层：`_build_initial_state()` 注入 worktree 路径 |
| `harness/main.py` | 启动层：加载配置 + stale cleanup |

---

## 三层调用关系

```
main.py                          ← 系统初始化层
  │
  ├─ WorktreeConfig 加载配置
  ├─ SubagentManager(worktree_config=...)
  └─ _cleanup_stale_worktrees()   ← 启动时一次性清理
        │
        ▼
subagent_manager.py              ← 编排层
  │
  └─ execute()
        ├─ ensure_git_repo()              (首次调用时懒初始化)
        ├─ create(name)                   (创建 worktree, 在 Semaphore 之前)
        ├─ SubagentExecutor(worktree_ctx) (传递上下文)
        ├─ executor.execute(task)         (Agent 在 worktree 中执行)
        └─ finally:
              ├─ merge(ctx)               (合并回主分支)
              └─ cleanup(ctx)             (销毁 worktree)
        │
        ▼
subagent_executor.py             ← Agent 执行层
  │
  └─ _build_initial_state(task)
        └─ 注入 [WORKTREE] 指令块到 HumanMessage
```

---

## 第 0 层：系统启动

### 调用路径

```
HarnessService.initialize()
  └─ main.py 步骤 8.5
```

### 配置加载

```python
# main.py — 从 config.yaml 读取 worktree 段
_wt_raw = self.config_manager.get("worktree") or {}
_wt_cfg = WorktreeConfig(
    enabled         = _wt_raw.get("enabled", True),
    auto_init       = _wt_raw.get("auto_init", True),
    symlink_deps    = _wt_raw.get("symlink_deps", [".venv", "node_modules"]),
    keep_on_conflict = _wt_raw.get("keep_on_conflict", True),
    cleanup_stale_on_start = _wt_raw.get("cleanup_stale_on_start", True),
)
```

```yaml
# config.yaml
worktree:
  enabled: true
  auto_init: true
  symlink_deps: [".venv", "node_modules", "__pycache__"]
  keep_on_conflict: true
  cleanup_stale_on_start: true
```

### 传递到 SubagentManager

```python
self.subagent_manager = SubagentManager(
    llm_factory=self._init_llm,
    tool_registry=self.tool_registry,
    max_concurrent=cfg.max_concurrent_subagents,
    skill_storage=self.skill_storage,
    worktree_config=_wt_cfg,    # ← 传入
)
```

### 启动时 Stale Cleanup

```python
# 紧接着 SubagentManager 创建之后
if _wt_cfg.enabled and _wt_cfg.cleanup_stale_on_start:
    await self._cleanup_stale_worktrees()
```

**关键设计**：`_worktree_mgr` 在 `SubagentManager` 中初始化为 `None`——懒初始化。只有首次遇到 `isolation: worktree` 的 SubAgent 时才真正创建 `GitWorktreeManager` 实例。避免在未启用 worktree 的系统上创建不必要的 git repo。

---

## 第 1 层：仓库初始化 — `ensure_git_repo()`

### 函数签名

```python
async def ensure_git_repo(self) -> None:
```

### 执行流程

```
ensure_git_repo()
│
├─ 1. 检查 workspace/.git 是否存在
│     └─ 已存在 → return (幂等性保证)
│
├─ 2. 检查 auto_init 开关
│     └─ False → raise RuntimeError("workspace is not a git repo")
│
├─ 3. git init
│     └─ 在 workspace 根目录初始化空 git 仓库
│
├─ 4. 创建 .gitignore，追加 ".worktrees/"
│     └─ 确保 worktree 管理目录不会被 git 跟踪
│
├─ 5. git add -A
│     └─ 暂存 workspace 中所有现有文件
│
└─ 6. git commit --allow-empty -m "Initial commit (auto)"
      └─ 创建一个基准 commit，让 git worktree add 有 ref 可以分支
```

### 关键细节

**`--allow-empty` 的必要性**：`git worktree add -b new-branch` 需要一个 commit 来创建分支。如果 workspace 是空目录，`git commit` 会失败（没有变更）。`--allow-empty` 允许创建空提交，确保即使 workspace 完全为空也能正常创建 worktree。

**幂等性**：通过 `workspace/.git` 目录存在性检查实现。由于只读检查（无副作用），多个并发调用都是安全的——第一个调用创建 repo，后续调用立即返回。

**`.gitignore` 追加而非覆盖**：使用 `_ensure_gitignored()` 方法，读取现有内容后追加，不会覆盖用户已有的 `.gitignore` 配置。

---

## 第 2 层：Worktree 创建 — `create()`

### 函数签名

```python
async def create(self, name: str) -> WorktreeContext:
```

### 执行流程

```
create(name: str) → WorktreeContext
│
├─ 1. _validate_name(name)
│     ├─ 正则: ^[a-z0-9_-]{1,64}$
│     └─ 拒绝: 空字符串、超长、../、空格、特殊字符
│
├─ 2. 确保 .worktrees/ 目录存在 + .gitignore 包含该模式
│
├─ 3. 生成唯一标识
│     ├─ uid = uuid.uuid4().hex[:6]        → "a1b2c3"
│     ├─ safe_name = f"{name}_{uid}"       → "coder_a1b2c3"
│     ├─ branch = f"subagent/{name}/{uid}" → "subagent/coder/a1b2c3"
│     └─ path = .worktrees/coder_a1b2c3/
│
├─ 4. 冲突处理
│     └─ path 已存在 (上次崩溃残留) → shutil.rmtree 删除
│
├─ 5. git worktree add {path} -b {branch}
│     ├─ 在 .worktrees/ 下创建独立工作目录
│     ├─ 创建新分支 subagent/coder/a1b2c3
│     └─ 基于当前 HEAD (初始 commit) 分支
│
├─ 6. 环境初始化 (best-effort, 失败不阻塞)
│     ├─ _symlink_deps(path)
│     │     └─ 对 .venv, node_modules, __pycache__ 创建符号链接
│     │
│     └─ _copy_configs(path)
│           └─ 复制 .env, .env.local, config.local.yaml
│
└─ 7. 返回 WorktreeContext
      ├─ name:       "coder"
      ├─ path:       /home/.../workspace/.worktrees/coder_a1b2c3/
      ├─ branch:     "subagent/coder/a1b2c3"
      └─ virtual_path: "/mnt/user-data/workspace/.worktrees/coder_a1b2c3/"
```

### 关键细节

**命名空间隔离**：分支名采用 `subagent/{name}/{uid}` 三层结构。`subagent/` 前缀将 SubAgent 的分支与用户分支（main/master/feature-*）隔离，`git branch` 列表不会混乱。

**UUID 短码**：`uuid4().hex[:6]` 给出 16^6 ≈ 1.6×10^7 种组合。即使同一 SubAgent 被连续调用 10 次，碰撞概率可以忽略不记。

**路径遍历防护**：`_validate_name()` 是第二层防护。虽然 `SubAgentConfig.name` 已有 Pydantic 校验，但 LLM 生成的 tool call 可能绕过。这里用正则白名单确保 name 只包含 `[a-z0-9_-]`，无法注入 `../../../etc/passwd`。

**符号链接 vs 拷贝**：`node_modules` 动辄 200MB+、`.venv` 可能 500MB+。`Path.symlink_to(target_is_directory=True)` 创建符号链接，节省磁盘空间，且两边看到的是同一个目录。对于 `__pycache__`，符号链接还能避免 Python 字节码缓存污染 worktree。

**`asyncio.to_thread(shutil.rmtree)`**：`rmtree` 是同步阻塞操作，放在 `to_thread` 中执行避免阻塞事件循环。git 命令通过 `asyncio.create_subprocess_exec` 异步执行，同样不阻塞。

---

## 第 3 层：上下文注入 — `_build_initial_state()`

### 函数签名

```python
def _build_initial_state(self, task: str) -> dict[str, Any]:
```

### 执行流程

```
_build_initial_state(task)
│
├─ 如果有 worktree_ctx:
│     task = f"""
│     [WORKTREE]
│     工作目录: /mnt/user-data/workspace/.worktrees/coder_a1b2c3/
│     分支: subagent/coder/a1b2c3
│     所有文件操作请在此目录下进行，不要修改主 workspace 的文件。
│     [/WORKTREE]
│
│     {原始 task}
│     """
│
├─ 构造 messages 列表:
│     [SkillMessage*, SystemMessage(system_prompt), HumanMessage(task)]
│
└─ 继承 parent_state:
      ├─ sandbox     ← 复用父级沙箱 (worktree 在已有挂载内)
      ├─ thread_data ← 路径映射一致性
      ├─ thread_id   ← 目录初始化
      └─ user_id     ← 权限隔离
```

### 关键细节

**透明注入**：SubAgent 不需要知道 worktree 的存在。`[WORKTREE]` 块是给 LLM 看的自然语言指令。LLM 看到"你的工作目录在这里"，自然会将 `file_write`、`bash` 等操作定向到 worktree 路径。

**沙箱兼容性**：worktree 目录在 `/mnt/user-data/workspace/.worktrees/` 下。沙箱已经将 `/mnt/user-data/workspace/` 挂载到容器/本地路径，所以 worktree 中的文件对沙箱工具天然可见，`file_read`、`file_write`、`bash` 直接可用。

**不修改 LangGraph State**：worktree 上下文通过 HumanMessage 注入，不进入 State Schema。这意味着不会触发 msgpack 序列化、不影响 Checkpointer、不污染 State Reducer。

---

## 第 4 层：变更合并 — `merge()`

### 函数签名

```python
async def merge(self, ctx: WorktreeContext) -> MergeResult:
```

### 执行流程

```
merge(ctx) → MergeResult
│
├─ async with self._merge_lock:        ← 同一 repo 的 merge 串行化
│
├─ 1. _get_default_branch()
│     ├─ git rev-parse --abbrev-ref HEAD  → 当前分支名
│     └─ fallback: main → master → "main"
│
├─ 2. git add -A (cwd=worktree_path)
│     └─ 暂存 worktree 中所有变更
│
├─ 3. git commit --allow-empty (cwd=worktree_path)
│     └─ 提交信息: "subagent(coder): automated commit"
│
├─ 4. 检测变更量
│     ├─ git rev-list --count {default_branch}..{branch}
│     └─ ahead == 0 → MergeResult(status="no_changes")
│
├─ 5. git checkout {default_branch} (cwd=workspace)
│     ├─ 成功 → 继续
│     └─ 失败 → git stash → 再试 checkout
│           └─ 再失败 → MergeResult(status="error")
│
├─ 6. git merge --no-ff {branch} (cwd=workspace)
│     ├─ 成功 → MergeResult(status="ok", files_changed=ahead)
│     └─ 冲突:
│           ├─ git diff --name-only --diff-filter=U  (收集冲突文件)
│           ├─ keep_on_conflict?
│           │     ├─ True  → 保留 worktree 现场，不做 abort
│           │     └─ False → git merge --abort
│           └─ MergeResult(status="conflict", conflict_files=[...])
│
└─ 释放 _merge_lock
```

### 关键细节

**`asyncio.Lock` 的必要性**：Git merge 操作在同一个 repo 上不能并发。如果两个 SubAgent 同时 merge，第二个 merge 会基于第一个 merge 的中间状态（可能正在 checkout 或尚未 commit），导致不可预期的行为。Lock 将 merge 变为严格串行。

**`--no-ff` 的原因**：`--no-ff`（no fast-forward）强制创建 merge commit，即使可以快进。这样每次 SubAgent 的变更有独立的 commit 和清晰的提交信息，通过 `git log` 可以追溯每个 SubAgent 的贡献。

**`rev-list --count A..B`**：Git range 语法 `A..B` 表示"B 有但 A 没有的 commits"。`--count` 返回数字。如果 B 分支基于 A 创建且没有任何新提交，返回 0 —— 表示 worktree 中没有任何有效变更。

**stash 回退策略**：主 workspace 可能有用户正在编辑但未提交的文件。`git checkout` 在 dirty working tree 上会失败。`git stash` 暂存变更后 checkout，不影响用户工作。这是 fail-safe 设计——尽最大努力不丢失用户数据。

**冲突处理**：当两个 SubAgent 修改了同一个文件的同一区域时发生冲突。处理策略：
- `keep_on_conflict=true`（默认）：保留 worktree 目录，不执行 `merge --abort`。worktree 中的 `.mine` 和 `.theirs` 文件可供人工检查
- `keep_on_conflict=false`：执行 `merge --abort` 回滚到 merge 前状态，丢弃 worktree 中的变更

---

## 第 5 层：清理销毁 — `cleanup()`

### 函数签名

```python
async def cleanup(self, ctx: WorktreeContext) -> None:
```

### 执行流程

```
cleanup(ctx)
│
├─ async with self._merge_lock:      ← 与 merge 共用锁，防止 race
│
├─ 1. git worktree remove --force {path}
│     ├─ --force: 即使有未提交变更也强制删除
│     └─ 失败 → shutil.rmtree 暴力清理磁盘目录
│
├─ 2. git branch -D {branch}
│     └─ 删除 SubAgent 的临时分支
│     └─ 失败 → 忽略 (分支可能已被 merge 删除)
│
└─ 3. 清理空目录
      └─ .worktrees/ 为空 → rmdir
```

### 关键细节

**与 merge 共用锁**：cleanup 和 merge 共用同一个 `_merge_lock`。如果 cleanup 在 merge 进行到一半时删除 worktree 目录，merge 会读到不完整的文件。共用锁确保 merge 完全结束后才清理。

**双重保险清理**：`git worktree remove` 清理 git 内部注册表，`shutil.rmtree` 作为 fallback 确保磁盘空间被释放。即使 git 元数据损坏，磁盘上的 worktree 目录也会被删除。

**分支静默删除**：`git branch -D` 失败时只是 `pass`，不抛异常。分支可能在 merge 时已自动删除（fast-forward merge），或者由于其他原因不存在。

---

## 第 6 层：残留清理 — `cleanup_stale()`

### 函数签名

```python
async def cleanup_stale(self) -> int:
```

### 执行流程

```
cleanup_stale() → int (清理数量)
│
├─ 1. git worktree prune
│     └─ 清理 git 内部注册表中已不存在的 worktree 引用
│
├─ 2. 对比 active worktrees 与 .worktrees/ 目录
│     ├─ list_worktrees() → 解析 git worktree list --porcelain
│     ├─ 遍历 .worktrees/ 下的每个子目录
│     └─ 不在 active 中的 → shutil.rmtree 删除
│
└─ 3. 清理孤立分支
      └─ git branch -D subagent/{entry_name}
```

### `list_worktrees()` — porcelain 格式解析

```
git worktree list --porcelain 输出示例:
────────────────────────────────────
worktree /home/user/workspace
HEAD abc123... refs/heads/main

worktree /home/user/workspace/.worktrees/coder_a1b2c3
HEAD def456... refs/heads/subagent/coder/a1b2c3
────────────────────────────────────

解析逻辑:
  遍历行 → 匹配 "worktree " 前缀 → 提取路径 → 判断是否在 .worktrees/ 下
```

### 关键细节

**三层清理保证无残留**：
1. `git worktree prune` — 清理 git 内部注册表
2. `shutil.rmtree` — 清理磁盘上的孤儿目录
3. `git branch -D` — 清理孤立分支引用

**启动时调用**：`main.py` 在 SubagentManager 创建后执行 `_cleanup_stale_worktrees()`。此时服务尚未接收请求，清理操作不影响线上。清理失败只记日志，不阻塞启动。

---

## 并发模型与时序

### 并发设计

```
操作           │ 并发度        │ 机制
───────────────┼──────────────┼────────────────────────
git init       │ 幂等          │ .git 存在则跳过
create()       │ 完全并发      │ git worktree add 内部有文件锁
work (Agent)   │ 完全并发      │ 独立目录，零竞争
merge()        │ 串行          │ asyncio.Lock per repo
cleanup()      │ 串行          │ 与 merge 共用锁
stale cleanup  │ 启动时单次    │ 无并发
```

### 时序图

```
                时间 ─────────────────────────────────────────▶

SubAgent A
  create wt_A ──┐
                │ 并发创建
SubAgent B      │
  create wt_B ──┤
                │
SubAgent C      │
  create wt_C ──┘
                │
                ▼
SubAgent A ── work in wt_A ── [申请 merge_lock] ── merge ── cleanup ── (释放)
SubAgent B ── work in wt_B ─────────────────────── [等待锁] ── merge ── cleanup
SubAgent C ── work in wt_C ───────────────────────────────────────── [等待] ── merge

                ↑ 并发创建+工作 (全速)    ↑ 串行 merge (~100ms/次)   ↑ 串行清理
```

### 为什么 merge 必须串行

```
场景 A (无锁，并发 merge):
  SubAgent A merge 到一半，正在 checkout main
  SubAgent B 同时 merge，基于 A 的中间状态
  → HEAD 指向不明，可能丢失 A 的变更

场景 B (有锁，串行 merge):
  SubAgent A merge 完成，释放锁
  SubAgent B 获取锁，看到 A 的最新状态
  → 每个 merge 基于完整的最新状态，安全
```

---

## 完整生命周期图

```
                    ┌──────────────────────────────────────────┐
                    │           main.py (系统启动)              │
                    │                                          │
                    │  加载 WorktreeConfig                     │
                    │  → SubagentManager(worktree_config=...)  │
                    │  → _cleanup_stale_worktrees()            │
                    └──────────────────┬───────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  SubagentManager.execute() 生命周期                       │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │ 1. 判断 isolation == "worktree" ?                               │    │
│  │    ├─ Yes → 2                                                   │    │
│  │    └─ No  → 跳过 worktree，直接创建 SubagentExecutor             │    │
│  ├─────────────────────────────────────────────────────────────────┤    │
│  │ 2. 懒初始化 (首次调用)                                           │    │
│  │    _worktree_mgr = GitWorktreeManager(workspace, config)        │    │
│  │    await _worktree_mgr.ensure_git_repo()                        │    │
│  ├─────────────────────────────────────────────────────────────────┤    │
│  │ 3. 创建 worktree (在 Semaphore 之前)                             │    │
│  │    worktree_ctx = await _worktree_mgr.create(name)              │    │
│  │    → git worktree add .worktrees/coder_a1b2c3/                 │    │
│  │    → symlink .venv, node_modules                                │    │
│  │    → copy .env, config.local.yaml                               │    │
│  ├─────────────────────────────────────────────────────────────────┤    │
│  │ 4. SubAgent 执行 (带 Semaphore)                                  │    │
│  │    executor = SubagentExecutor(worktree_ctx=ctx, ...)           │    │
│  │    → _build_initial_state(task)                                 │    │
│  │         └─ task = "[WORKTREE]\n工作目录: ...\n[/WORKTREE]\n"   │    │
│  │                  + task                                         │    │
│  │    → executor.execute(task)                                     │    │
│  │         └─ agent.astream(state) ─ ReAct 循环                    │    │
│  │              ├─ abefore_agent:  ThreadDataMiddleware(lazy_init) │    │
│  │              ├─ aafter_model:   LoopDetection                  │    │
│  │              ├─ awrap_tool_call: SandboxMiddleware              │    │
│  │              │     └─ file_write("/mnt/.../workspace/          │    │
│  │              │            .worktrees/coder_a1b2c3/main.py")    │    │
│  │              │           → sandbox.resolve_path                 │    │
│  │              │              → host worktree path                │    │
│  │              └─ ... (迭代直至完成或超时)                         │    │
│  ├─────────────────────────────────────────────────────────────────┤    │
│  │ 5. finally (无论成功/失败/取消)                                  │    │
│  │    ├─ await _worktree_mgr.merge(ctx)                           │    │
│  │    │     ├─ git add -A && git commit                           │    │
│  │    │     ├─ git checkout main                                  │    │
│  │    │     ├─ git merge --no-ff subagent/coder/a1b2c3            │    │
│  │    │     └─ → MergeResult(status="ok")                         │    │
│  │    └─ await _worktree_mgr.cleanup(ctx)                         │    │
│  │          ├─ git worktree remove --force                        │    │
│  │          └─ git branch -D subagent/coder/a1b2c3                │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘

                              ▲
                              │ git 操作
                              │
┌─────────────────────────────────────────────────────────────────┐
│                  GitWorktreeManager 内部方法                      │
│                                                                 │
│  create(name)          ensure_git_repo()     merge(ctx)         │
│    ├─ _validate_name     ├─ git init           ├─ _get_default   │
│    ├─ _ensure_gitignored ├─ .gitignore           _branch()       │
│    ├─ git worktree add   └─ git commit          ├─ git add -A    │
│    ├─ _symlink_deps          --allow-empty      ├─ git commit    │
│    └─ _copy_configs                              ├─ git rev-list │
│                                                  ├─ git checkout │
│  cleanup_stale()          cleanup(ctx)           ├─ git merge    │
│    ├─ git worktree prune   ├─ git worktree       └─ _get_conflict │
│    ├─ 对比 active list        remove --force        _files()     │
│    └─ rmtree orphans        ├─ git branch -D                    │
│                             └─ rmdir .worktrees                 │
│                                                                 │
│  _run_git(*args, cwd)          _run_git_impl()                  │
│    └─ 异步子进程执行 git       └─ asyncio.create_subprocess_exec │
│       raise on failure            ├─ stdout/stderr PIPE          │
│                                   └─ returncode != 0 → raise    │
└─────────────────────────────────────────────────────────────────┘
```

---

## SubagentManager.execute() 完整伪代码

```python
async def execute(self, name, instruction, context, parent_state):
    # 1. 查找 SubAgent 配置
    config, tools, llm = self._agents[name]

    # 2. 判断是否需要 worktree 隔离
    _worktree_enabled = (
        config.isolation == "worktree"
        and self._worktree_config is not None
        and self._worktree_config.enabled
    )

    worktree_ctx = None
    if _worktree_enabled:
        # 2a. 懒初始化 GitWorktreeManager
        if self._worktree_mgr is None:
            self._worktree_mgr = GitWorktreeManager(workspace, config)
            await self._worktree_mgr.ensure_git_repo()

        # 2b. 创建 worktree (在 Semaphore 之前，不占并发槽)
        worktree_ctx = await self._worktree_mgr.create(name)

    try:
        # 3. Semaphore 控制 SubAgent 并发数
        async with self._semaphore:
            executor = SubagentExecutor(
                config, llm, tools, parent_state,
                worktree_ctx=worktree_ctx,  # ← 传入 worktree 上下文
            )
            # 4. 在独立线程中执行 (executor.execute 是同步阻塞的)
            result = await asyncio.to_thread(executor.execute, instruction)
            return result
    finally:
        # 5. 无论如何都执行 merge + cleanup
        if worktree_ctx is not None:
            await self._worktree_mgr.merge(worktree_ctx)
            await self._worktree_mgr.cleanup(worktree_ctx)
```

---

## 数据模型

### WorktreeContext

```python
@dataclass
class WorktreeContext:
    name: str           # SubAgent 名称, e.g. "coder"
    path: Path          # 主机绝对路径, e.g. /home/.../.worktrees/coder_a1b2c3/
    branch: str         # Git 分支名, e.g. "subagent/coder/a1b2c3"
    virtual_path: str   # 沙箱内路径, e.g. "/mnt/user-data/workspace/.worktrees/coder_a1b2c3/"
```

### MergeResult

```python
@dataclass
class MergeResult:
    status: Literal["ok", "conflict", "no_changes", "error"]
    files_changed: int = 0
    conflict_files: list[str] = field(default_factory=list)
    summary: str = ""
```

### WorktreeConfig

```python
@dataclass
class WorktreeConfig:
    enabled: bool = True
    auto_init: bool = True
    symlink_deps: list[str] = [".venv", "node_modules"]
    keep_on_conflict: bool = True
    cleanup_stale_on_start: bool = True
```

### SubAgentConfig (新增字段)

```python
class SubAgentConfig(BaseModel):
    # ... 原有字段 ...
    isolation: str = "none"  # "none" | "worktree"
```

---

## 技术知识点汇总

### Git 相关

| 知识点 | 应用函数 | 说明 |
|--------|---------|------|
| `git worktree add` | `create()` | 为一个 repo 创建额外的独立工作目录，共享 `.git/objects` 和 `.git/refs`，各自有独立的 HEAD、index 和 working tree |
| `git worktree remove` | `cleanup()` | 删除 worktree 目录并清理 git 内部注册表。`--force` 忽略未提交变更 |
| `git worktree prune` | `cleanup_stale()` | 清理 git 注册表中已不存在的 worktree 引用 |
| `git worktree list --porcelain` | `list_worktrees()` | 机器可读格式列出所有 worktree，格式为 `worktree <path>` / `HEAD <hash>` / `branch <ref>` |
| `git rev-list --count A..B` | `_merge_impl()` | 统计 B 分支领先 A 分支的 commit 数量，用于判断是否有变更需要 merge |
| `git merge --no-ff` | `_merge_impl()` | 强制创建 merge commit（即使可以快进），保留分支历史和可追溯的提交信息 |
| `git commit --allow-empty` | `ensure_git_repo()`, `_merge_impl()` | 允许创建空提交，用于空目录初始化或无变更的 worktree |
| `git stash` | `_merge_impl()` | 暂存未提交变更，checkout 失败时的回退策略 |
| `git diff --name-only --diff-filter=U` | `_get_conflict_files()` | 列出所有有 merge 冲突的文件（`--diff-filter=U` 只显示 unmerged 文件） |
| `--no-ff merge` | `_merge_impl()` | 对比 `git merge`（默认允许 fast-forward），`--no-ff` 确保 merge 后的提交图保留分支路径，便于追溯变更来源 |

### Python 异步编程

| 知识点 | 应用位置 | 说明 |
|--------|---------|------|
| `asyncio.Lock` | `merge()`, `cleanup()` | 异步互斥锁。`async with lock:` 语法，阻塞协程而非线程，适合 I/O 密集型场景。同一 repo 的 merge 必须串行化 |
| `asyncio.Semaphore` | `SubagentManager.execute()` | 控制同时运行的 SubAgent 数量（2-4）。`async with semaphore:` 获取槽位，超出限制的协程挂起等待 |
| `asyncio.to_thread()` | `create()`, `cleanup()` | 将同步阻塞函数（如 `shutil.rmtree`）放到线程池执行，返回可 await 的 coroutine |
| `asyncio.create_subprocess_exec()` | `_run_git_impl()` | 异步创建子进程，stdout/stderr 通过 `asyncio.subprocess.PIPE` 捕获。不阻塞事件循环 |
| `proc.communicate()` | `_run_git_impl()` | 等待子进程结束并读取 stdout/stderr |

### 并发设计

| 知识点 | 说明 |
|--------|------|
| 懒初始化 (Lazy Init) | `_worktree_mgr` 在首次使用时创建，避免未启用 worktree 时的不必要初始化。通过检查 `self._worktree_mgr is None` 实现 |
| 信号量外创建 | worktree 创建在 `async with self._semaphore` **之前**执行。创建操作不占用 SubAgent 并发槽位，不影响其他 SubAgent 的工作 |
| 锁内 Merge | merge 在 `async with self._merge_lock` 内执行。同一 repo 的 merge 严格串行，避免并发 merge 的中间状态污染 |
| try/finally 保证 Cleanup | cleanup 放在 `finally` 块中，无论 SubAgent 正常完成、异常失败还是被取消，worktree 都会被清理 |
| 故障降级 | `create()` 失败时 catch 异常、记录日志，SubAgent 回退到共享 workspace 继续执行 |

### 安全防护

| 知识点 | 应用函数 | 说明 |
|--------|---------|------|
| 路径遍历防护 | `_validate_name()` | 正则 `[a-z0-9_-]` 白名单，拒绝 `.`、`..`、空格、特殊字符。LLM 注入的恶意路径会被拒绝 |
| 防御性编程 | `create()` | worktree 目录已存在时先删除再创建。防止上次崩溃的残留导致 `git worktree add` 失败 |
| 原子写入 | N/A (git 层面) | `git worktree add` 内部使用文件锁，并发创建同一目录时自动排队 |
| 环境变量展开 | `SubagentManager` | 从 `config.yaml` 读取配置时不做环境变量展开，符号链接和拷贝操作使用绝对路径 |
| 双重保险清理 | `cleanup()` | `git worktree remove` 失败后，fallback 到 `shutil.rmtree` 直接删除磁盘目录 |

### Git Worktree 原理

```
单一 Git 仓库
═══════════════════════════════════════════════════════════════
  workspace/
  ├── .git/
  │     ├── objects/          ← 所有 worktree 共享 (commits, trees, blobs)
  │     ├── refs/             ← 所有 worktree 共享 (branches, tags)
  │     └── worktrees/        ← 每个 worktree 的独立元数据
  │           └── coder_a1b2c3/
  │                 ├── HEAD       → refs/heads/subagent/coder/a1b2c3
  │                 ├── index      ← 独立暂存区
  │                 └── ORIG_HEAD
  ├── main.py                 ← 主 workspace 的文件
  └── .worktrees/
        ├── coder_a1b2c3/     ← SubAgent A 的独立工作树
        │     ├── .git → ../../.git     (符号链接指向主 .git)
        │     └── output.py             (SubAgent 创建的文件)
        └── researcher_d4e5f6/ ← SubAgent B 的独立工作树
              ├── .git → ../../.git
              └── research.md

关键原理:
  - .git/objects 和 .git/refs 共享 → 节省磁盘空间
  - 每个 worktree 有独立的 HEAD、index → 文件操作完全隔离
  - worktree 内的 .git 不是目录，是指向主 .git 的文本文件
    (内容: "gitdir: /path/to/main/.git/worktrees/coder_a1b2c3")
```

### 与 LangGraph State 的兼容性

| LangGraph 概念 | Worktree 交互 | 说明 |
|---------------|-------------|------|
| State Schema | 不修改 | Worktree 路径通过 HumanMessage 注入，不进入 State |
| Checkpointer | 不涉及 | Worktree 上下文是瞬态对象，不序列化到 checkpoint |
| State Reducer | 不涉及 | 无新字段进入 state，不影响现有 reducer |
| Middleware | 透明 | ThreadDataMiddleware(lazy_init=True) 继承父级 thread_data |
| SubAgent Graph | 独立 | SubAgent 是独立的 `create_agent()` 实例，state 与父 Agent 隔离 |
