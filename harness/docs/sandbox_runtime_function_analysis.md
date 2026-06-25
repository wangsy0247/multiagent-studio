# Harness 沙箱系统函数级运行分析

> 本文档描述改造后的 Harness 沙箱系统（DeerFlow 风格虚拟路径）从初始化到工具执行的完整函数调用链路。

---

## 一、总体架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         HarnessService                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ HarnessConfig │  │ ConfigManager │  │ ToolRegistry             │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────────┘  │
│         │                 │                      │                  │
│         ▼                 ▼                      ▼                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                  initialize() 初始化流程                       │  │
│  │  1. set_paths(Paths(data_root))                                │  │
│  │  2. 加载工具（含 sandbox_tools）                                │  │
│  │  3. _register_middlewares() → SandboxMiddleware                │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Agent 运行阶段                               │
│                                                                      │
│  Agent ──► bash/file_read/file_write/list_files/glob/grep/str_replace│
│            │                                                         │
│            ▼                                                         │
│  tools/sandbox_tools.py                                              │
│            │                                                         │
│            ├── _get_sandbox()                                        │
│            │       ├── get_sandbox_provider()                        │
│            │       │       └── 根据 config.sandbox_use 实例化 provider│
│            │       └── provider.acquire(thread_id, workspace)        │
│            │               └── LocalSandbox / DockerSandbox          │
│            │                                                         │
│            ├── _normalize_virtual_path(path)                         │
│            │                                                         │
│            ├── sandbox.execute_command/read_file/write_file/...      │
│            │       └── resolve_path() 虚拟路径 → 物理/容器路径        │
│            │                                                         │
│            └── sandbox.sanitize_output(output)                       │
│                    └── 物理/容器路径 → 虚拟路径                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、初始化阶段

### 2.1 HarnessService.initialize()

文件：`harness/main.py`

```python
async def initialize(self) -> None:
    cfg = self.config

    # 0. 初始化 Paths 单例并确保数据根目录存在
    set_paths(Paths(cfg.data_root))
    paths = get_paths()
    paths.ensure_data_dir()
    logger.info("Data root: %s", paths.base_dir)

    # 2. 从 config.yaml 加载工具
    if self.config_manager is not None:
        raw_tools = self.config_manager.get("tools", [])
        tool_configs = [ToolConfig(**t) for t in raw_tools]
        self.tool_registry.load_tools_from_config(tool_configs)

    # 7. 注册中间件
    self._register_middlewares()
```

关键动作：

1. `set_paths(Paths(cfg.data_root))` 设置全局 `Paths` 单例。
2. `paths.ensure_data_dir()` 创建 `~/.multiagent-studio/` 数据根目录。
3. 工具注册时，sandbox 工具从 `harness.tools.sandbox_tools` 加载。
4. `_register_middlewares()` 创建 `SandboxMiddleware` 并加入中间件链。

### 2.2 Paths 初始化

文件：`harness/config/paths.py`

```python
class Paths:
    def __init__(self, base_dir: str | Path | None = None):
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def base_dir(self) -> Path:
        if self._base_dir is not None:
            return self._base_dir
        return _default_local_base_dir()  # HarnessConfig().data_root
```

`_default_local_base_dir()` 读取 `HarnessConfig().data_root`（默认 `~/.multiagent-studio`）。

### 2.3 工具注册

文件：`harness/tools/registry.py`

```python
def load_tools_from_config(self, tools_config: list[ToolConfig] | None) -> list[BaseTool]:
    for cfg in tools_config:
        tool = resolve_variable(cfg.use, BaseTool)
        self.register(tool, category=cfg.group)
```

`sandbox_tools.py` 在模块级创建了工具实例：

```python
bash = create_bash_tool()
file_read = create_file_read_tool()
file_write = create_file_write_tool()
list_files = create_list_files_tool()
glob_tool = create_glob_tool()
grep_tool = create_grep_tool()
str_replace = create_str_replace_tool()
```

这些实例通过 `config.yaml` 中的 `use: harness.tools.sandbox_tools:bash` 被加载。

### 2.4 SandboxMiddleware 注册

文件：`harness/middleware/sandbox.py`

```python
class SandboxMiddleware(HarnessAgentMiddleware):
    name = "sandbox"

    async def abefore_agent(self, state: HarnessState, runtime: Runtime):
        _set_context_from_state(state)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        state = getattr(request, "state", None)
        if state is None and isinstance(request, dict):
            state = request.get("state")
        _set_context_from_state(state)
        return await handler(request)
```

中间件在 Agent 每次调用工具前注入 `thread_id` 和 `workspace` 到 `ContextVar`。

---

## 三、运行时阶段：Agent 调用工具

### 3.1 入口示例：bash 工具

文件：`harness/tools/sandbox_tools.py`

```python
@tool
async def bash(command: str, timeout: int = 30) -> str:
    sandbox = await _get_sandbox()
    output = await sandbox.execute_command(command, timeout=timeout)
    return sandbox.sanitize_output(output)
```

### 3.2 获取沙箱：_get_sandbox()

```python
async def _get_sandbox() -> Sandbox:
    ctx = _current_ctx()
    workspace = ctx.get("workspace") or "."
    thread_id = ctx.get("thread_id") or "default"
    provider = get_sandbox_provider()
    return await provider.acquire(thread_id, workspace)
```

流程：

1. 从 `ContextVar` 读取 `thread_id` / `workspace`。
2. 调用 `get_sandbox_provider()` 获取单例 provider。
3. 调用 `provider.acquire(thread_id, workspace)` 获取/复用沙箱。

### 3.3 Provider 选择：get_sandbox_provider()

文件：`harness/services/sandbox_provider.py`

```python
def get_sandbox_provider(**kwargs: Any) -> SandboxProvider:
    cfg = load_config()
    use = cfg.sandbox_use if hasattr(cfg, "sandbox_use") else ""

    if not use:
        from harness.services.local_sandbox_provider import LocalSandboxProvider
        return LocalSandboxProvider(**kwargs)

    from harness.utils import resolve_variable
    provider_cls = resolve_variable(use, SandboxProvider)
    return provider_cls(**kwargs)
```

- `config.yaml` 中 `sandbox.use` 为空 → `LocalSandboxProvider`
- `sandbox.use` 为 `harness.services.docker_sandbox_provider:DockerSandboxProvider` → `DockerSandboxProvider`

---

## 四、LocalSandboxProvider 详细流程

### 4.1 provider.acquire()

文件：`harness/services/local_sandbox_provider.py`

```python
class LocalSandboxProvider(SandboxProvider):
    async def acquire(self, thread_id: str, workspace: str) -> Sandbox:
        return LocalSandbox(thread_id)
```

每次返回新的 `LocalSandbox` 实例。

### 4.2 LocalSandbox 初始化

```python
class LocalSandbox(Sandbox):
    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.path_mappings = self._build_path_mappings()
```

`_build_path_mappings()` 创建映射：

```python
def _build_path_mappings(self) -> list[PathMapping]:
    paths = get_paths()
    paths.ensure_thread_dirs(self.thread_id)
    return [
        PathMapping("/mnt/user-data/workspace", paths.sandbox_work_dir(self.thread_id)),
        PathMapping("/mnt/user-data/uploads",   paths.sandbox_uploads_dir(self.thread_id)),
        PathMapping("/mnt/user-data/outputs",   paths.sandbox_outputs_dir(self.thread_id)),
        PathMapping("/mnt/user-data",           paths.sandbox_user_data_dir(self.thread_id)),
        PathMapping("/mnt/acp-workspace",       paths.acp_workspace_dir(self.thread_id)),
    ]
```

生成的目录结构：

```text
~/.multiagent-studio/
└── threads/
    └── {thread_id}/
        ├── user-data/
        │   ├── workspace/
        │   ├── uploads/
        │   └── outputs/
        └── acp-workspace/
```

### 4.3 虚拟路径 → 物理路径：resolve_path()

```python
def resolve_path(self, virtual_path: str) -> str:
    mapping = self._find_mapping(virtual_path)
    if mapping is None:
        # 相对路径 → 默认 workspace
        mapping = self.path_mappings[0]  # /mnt/user-data/workspace
        relative = virtual_path
    else:
        relative = virtual_path[len(mapping.container_path):].lstrip("/")

    target = (mapping.local_path / relative).resolve()
    target.relative_to(mapping.local_path.resolve())  # 越界检查
    return str(target)
```

示例：

```text
输入:  /mnt/user-data/workspace/foo.txt
映射:  /mnt/user-data/workspace → ~/.multiagent-studio/threads/tid/user-data/workspace
输出:  ~/.multiagent-studio/threads/tid/user-data/workspace/foo.txt
```

### 4.4 bash 命令中的路径替换

```python
async def execute_command(self, command, timeout=30):
    resolved_command = self._resolve_paths_in_command(command)
    workspace = self.resolve_path("/mnt/user-data/workspace")
    proc = await asyncio.create_subprocess_shell(
        resolved_command,
        cwd=workspace,
        ...
    )
```

`_resolve_paths_in_command()` 用正则匹配命令中的虚拟路径，逐个替换为物理路径。

示例：

```text
命令: cat /mnt/user-data/workspace/foo.txt
替换后: cat /home/user/.multiagent-studio/threads/tid/user-data/workspace/foo.txt
```

### 4.5 文件操作

```python
async def read_file(self, path: str) -> str:
    target = Path(self.resolve_path(path))
    return target.read_text(...)

async def write_file(self, path: str, content: str) -> None:
    target = Path(self.resolve_path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, ...)
```

### 4.6 输出脱敏：sanitize_output()

```python
def sanitize_output(self, output: str) -> str:
    result = output
    for mapping in sorted(self.path_mappings, key=len, reverse=True):
        local_str = str(mapping.local_path.resolve())
        pattern = re.compile(re.escape(local_str) + r"(?:[/\\][^\s\"';&|<>()]*)?")
        result = pattern.sub(replace_match, result)
    result = result.replace(str(get_paths().base_dir.resolve()), "<data-root>")
    result = result.replace(os.path.expanduser("~"), "~")
    return result
```

示例：

```text
原始输出: /home/user/.multiagent-studio/threads/tid/user-data/workspace/foo.txt
脱敏后:  /mnt/user-data/workspace/foo.txt
```

### 4.7 glob / grep 中的虚拟路径

`glob()` 和 `grep()` 遍历物理目录，但返回虚拟路径：

```python
base_virtual = self._reverse_resolve(str(target))
for ...:
    matches.append(f"{base_virtual}/{rel_str}")
```

---

## 五、DockerSandboxProvider 详细流程

### 5.1 provider.acquire()

```python
class DockerSandboxProvider(SandboxProvider):
    async def acquire(self, thread_id: str, workspace: str) -> Sandbox:
        mounts = self._get_thread_mounts(thread_id)
        await self.service.get_or_create(thread_id, workspace, mounts=mounts)
        return DockerSandbox(thread_id, self.service)
```

### 5.2 构建挂载点

```python
def _get_thread_mounts(self, thread_id: str) -> list[tuple[str, str, bool]]:
    paths = get_paths()
    paths.ensure_thread_dirs(thread_id)
    return [
        (paths.host_sandbox_work_dir(thread_id),   "/mnt/user-data/workspace", False),
        (paths.host_sandbox_uploads_dir(thread_id), "/mnt/user-data/uploads",   False),
        (paths.host_sandbox_outputs_dir(thread_id), "/mnt/user-data/outputs",   False),
        (paths.host_acp_workspace_dir(thread_id),   "/mnt/acp-workspace",       True),
    ]
```

### 5.3 启动容器：SandboxService.get_or_create()

文件：`harness/services/sandbox.py`

```python
async def get_or_create(self, thread_id, workspace, mounts=None):
    if thread_id in self._pool:
        return self._pool[thread_id]

    # 构建 Docker volumes 字典
    volumes = {}
    for host_path, container_path, read_only in mounts:
        host_abs = str(Path(host_path).resolve())
        mode = "ro" if read_only else "rw"
        volumes[host_abs] = {"bind": container_path, "mode": mode}

    container = await loop.run_in_executor(
        None,
        lambda: client.containers.run(
            image=self.image,
            command="sleep infinity",
            volumes=volumes,
            working_dir="/mnt/user-data/workspace",
            ...
        ),
    )
    self._pool[thread_id] = container
```

实际生成的 Docker 命令：

```bash
docker run \
  --rm -d \
  -v /home/user/.multiagent-studio/threads/tid/user-data/workspace:/mnt/user-data/workspace:rw \
  -v /home/user/.multiagent-studio/threads/tid/user-data/uploads:/mnt/user-data/uploads:rw \
  -v /home/user/.multiagent-studio/threads/tid/user-data/outputs:/mnt/user-data/outputs:rw \
  -v /home/user/.multiagent-studio/threads/tid/acp-workspace:/mnt/acp-workspace:ro \
  -w /mnt/user-data/workspace \
  python:3.11-slim
```

### 5.4 DockerSandbox 路径处理

```python
class DockerSandbox(Sandbox):
    def resolve_path(self, virtual_path: str) -> str:
        if not virtual_path.startswith(("/mnt/user-data", "/mnt/acp-workspace")):
            if not virtual_path.startswith("/"):
                return f"/mnt/user-data/workspace/{virtual_path}"
            raise ValueError(...)
        return virtual_path
```

因为 bind mount 已经映射好，容器内路径就是虚拟路径本身。

### 5.5 DockerSandbox 命令执行

```python
async def execute_command(self, command, timeout=30):
    return await self.service.execute(self.thread_id, command, timeout=timeout)
```

命令直接发给容器执行，容器内工作目录是 `/mnt/user-data/workspace`。

### 5.6 DockerSandbox 文件操作

```python
async def read_file(self, path: str) -> str:
    container_path = self._container_path(path)
    result = await self.service.execute(self.thread_id, f"cat {shlex.quote(container_path)}")
    ...
```

### 5.7 DockerSandbox 输出脱敏

```python
def sanitize_output(self, output: str) -> str:
    result = output.replace(os.path.expanduser("~"), "~")
    result = result.replace(str(get_paths().base_dir.resolve()), "<data-root>")
    return result
```

容器内输出通常已经是虚拟路径，所以脱敏较简单。

---

## 六、完整调用示例

### 6.1 bash 工具完整链路（LocalSandbox）

```text
Agent: bash(command="cat /mnt/user-data/workspace/foo.txt")
       │
       ▼
tools/sandbox_tools.py::bash()
       │
       ├── _get_sandbox()
       │       ├── get_sandbox_provider() → LocalSandboxProvider()
       │       └── provider.acquire("thread_abc", ".") → LocalSandbox("thread_abc")
       │
       ├── sandbox.execute_command("cat /mnt/user-data/workspace/foo.txt")
       │       ├── _resolve_paths_in_command()
       │       │       └── "cat /home/user/.multiagent-studio/threads/thread_abc/user-data/workspace/foo.txt"
       │       ├── create_subprocess_shell(command, cwd=workspace_physical_path)
       │       └── return "Hello World"
       │
       └── sandbox.sanitize_output("Hello World")
               └── "Hello World" (无路径，无需脱敏)
```

### 6.2 file_read 完整链路（DockerSandbox）

```text
Agent: file_read(path="/mnt/user-data/workspace/foo.txt")
       │
       ▼
tools/sandbox_tools.py::file_read()
       │
       ├── _normalize_virtual_path("/mnt/user-data/workspace/foo.txt")
       │       └── "/mnt/user-data/workspace/foo.txt"
       │
       ├── _get_sandbox() → DockerSandbox("thread_abc")
       │
       ├── sandbox.read_file("/mnt/user-data/workspace/foo.txt")
       │       ├── resolve_path() → "/mnt/user-data/workspace/foo.txt"
       │       ├── service.execute("cat '/mnt/user-data/workspace/foo.txt'")
       │       └── return "Hello World"
       │
       └── sandbox.sanitize_output("Hello World")
               └── "Hello World"
```

---

## 七、生命周期与资源管理

### 7.1 沙箱获取/复用

| Provider | acquire 行为 | release 行为 |
|---|---|---|
| LocalSandboxProvider | 每次新建 LocalSandbox | 无操作 |
| DockerSandboxProvider | 按 thread_id 复用容器 | 停止并清理容器 |

### 7.2 容器池上限

`SandboxService` 中：

```python
MAX_CONTAINERS = 50
if len(self._pool) >= MAX_CONTAINERS:
    oldest_tid = next(iter(self._pool))
    await self.cleanup(oldest_tid)
```

### 7.3 数据持久化

- thread 数据保存在 `~/.multiagent-studio/threads/{thread_id}/`。
- 容器停止后数据仍然保留在主机上。
- 可以通过 `Paths.delete_thread_dir(thread_id)` 清理。

---

## 八、关键函数速查表

| 函数 | 文件 | 作用 |
|---|---|---|
| `set_paths()` | `config/paths.py` | 设置全局 Paths 单例 |
| `get_paths()` | `config/paths.py` | 获取 Paths 单例 |
| `ensure_thread_dirs()` | `config/paths.py` | 创建线程数据目录 |
| `resolve_virtual_path()` | `config/paths.py` | 虚拟路径转主机路径 |
| `get_sandbox_provider()` | `services/sandbox_provider.py` | 获取 provider 单例 |
| `provider.acquire()` | provider 实现 | 获取沙箱 |
| `sandbox.resolve_path()` | provider 实现 | 虚拟路径转执行路径 |
| `sandbox.sanitize_output()` | provider 实现 | 输出脱敏 |
| `execute_command()` | provider 实现 | 执行命令 |
| `read_file()` / `write_file()` | provider 实现 | 文件读写 |
| `list_dir()` / `glob()` / `grep()` | provider 实现 | 目录/搜索 |
| `set_sandbox_tool_context()` | `tools/sandbox_tools.py` | 设置工具上下文 |
| `_normalize_virtual_path()` | `tools/sandbox_tools.py` | 规范化用户路径 |
| `_set_context_from_state()` | `middleware/sandbox.py` | 中间件注入上下文 |
