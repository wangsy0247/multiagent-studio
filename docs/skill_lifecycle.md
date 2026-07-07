# MultiAgent-Studio Skill 完整生命周期文档

## 生命周期总览

```
Discovery → Parsing → Validation → Security Scan → Storage → Cache → Injection → Filtering → Execution → Management
```

---

## 1. Discovery（发现）

扫描 `skills/public/` 和 `skills/custom/` 目录，跳过隐藏目录（`.` 开头）。

**兜底策略**: 根目录不存在则自动创建 `public/` 和 `custom/` 子目录。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `SkillStorage._iter_skill_files()` | `harness/skills/storage.py` | 99 | 遍历 `public/` 和 `custom/`，跳过隐藏目录，yield `(category, category_root, skill_md_path)` |
| `SkillStorage.load_skills(enabled_only=False)` | `harness/skills/storage.py` | 122 | 调用 `_iter_skill_files()` 发现所有 SKILL.md，解析后合并 `extensions_config.json` 启用状态 |
| `HarnessService.initialize()` | `harness/main.py` | 211–219 | 创建 `skills/` 目录结构，初始化 `SkillStorage`，加载已启用 skill |

---

## 2. Parsing（解析）

解析 `SKILL.md` 文件的 YAML frontmatter（`---` 分隔符之间的内容）。

提取字段: `name`, `description`, `license`, `allowed-tools`, `metadata`, `compatibility`, `version`, `author`。

**兜底策略**: 格式错误 / 缺少必需字段 / 非法 YAML → 返回 `None`，静默跳过该 skill，不影响其他 skill 加载。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `parse_skill_file(skill_file, category, relative_path=None)` | `harness/skills/parser.py` | 44 | 读取 SKILL.md → 正则提取 YAML → `yaml.safe_load` → 构造 `Skill` 对象。失败返回 `None` |
| `parse_allowed_tools(raw, skill_file)` | `harness/skills/parser.py` | 14 | 解析 `allowed-tools` 字段。`None` = 全允许，`[]` = 显式禁止，`["a","b"]` = 白名单。非法值抛出 `ValueError` |

---

## 3. Validation（校验）

校验 SKILL.md frontmatter 的合法性。

规则:
- `name`: `^[a-z0-9]+(?:-[a-z0-9]+)*$`，最大 64 字符
- `description`: 最大 1024 字符，禁止 `<>` 尖括号
- `allowed-tools`: 必须是字符串列表，不能有空字符串
- 未知 key 拒绝（只允许 `ALLOWED_FRONTMATTER_PROPERTIES` 中的 8 个属性）

**兜底策略**: 校验失败 → 拒绝写入/安装，返回 400 错误。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `_validate_skill_frontmatter(skill_dir)` | `harness/skills/validation.py` | 34 | 返回 `(is_valid: bool, message: str, skill_name: str \| None)` |
| `SkillStorage.validate_skill_name(name)` | `harness/skills/storage.py` | 44 | 校验 name 符合 hyphen-case 规范，抛出 `ValueError` |

---

## 4. Security Scan（安全扫描）

使用 LLM 判断 skill 内容安全性，三级决策：`ALLOW` / `WARN` / `BLOCK`。

检测项:
1. **命令注入** — 执行任意用户输入的 shell 命令
2. **数据外泄** — 发送文件/环境变量/密钥到外部 URL
3. **权限提升** — sudo、chmod 777、setuid
4. **恶意载荷** — 混淆代码、反向 shell、挖矿程序
5. **路径穿越** — 读写指定目录外的文件
6. **社会工程** — 钓鱼、冒充、欺骗用户
7. **资源滥用** — 无限循环、fork 炸弹、填满磁盘

**兜底策略**:
- 模型客户端为 `None` → `BLOCK`
- 模型调用异常 → `BLOCK`
- 输出不可解析 → `BLOCK`
- `executable=True` 时 `WARN` → 升级为 `BLOCK`
- `executable=False` 的 SKILL.md，`WARN` 可放行

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `scan_skill_content(content, *, executable=False, model_client=None)` | `harness/skills/security_scanner.py` | 63 | 异步主入口，发送 system prompt + 内容到 LLM，解析返回的决策 |
| `_parse_scan_response(raw)` | `harness/skills/security_scanner.py` | 117 | 解析 LLM 响应中的 `ALLOW\|WARN\|BLOCK` 关键词，不可解析返回 `BLOCK` |
| `scan_skill_content_sync(content, ...)` | `harness/skills/security_scanner.py` | 156 | 同步包装器 |

### 数据类型

| 类型 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `ScanDecision(StrEnum)` | `harness/skills/security_scanner.py` | 128 | `ALLOW="allow"` / `WARN="warn"` / `BLOCK="block"` |
| `ScanResult(BaseModel)` | `harness/skills/security_scanner.py` | 133 | `decision` + `reason` + `is_allowed` + `is_blocked` 属性 |

---

## 5. Storage（持久化）

### 目录布局

```
skills/
├── public/<name>/SKILL.md     ← 内置 skill（git 跟踪，只读）
├── custom/<name>/SKILL.md     ← 用户 skill（.gitignored，可编辑）
└── custom/.history/<name>.jsonl  ← 变更历史
```

### 5.1 原子写入

使用 `tempfile.NamedTemporaryFile` + `os.replace` 保证原子性。路径穿越防护：写入前 `target.resolve()` 必须 `relative_to(skill_dir.resolve())`。

### 5.2 JSONL 历史

每次 mutate 操作追加一条 JSON 记录：`{"ts": "<ISO8601>", "action": "...", ...}`。

### 5.3 压缩包安装

`.skill` 文件是 ZIP 包，包含：
- `SKILL.md`（必需）
- `references/`、`templates/`、`scripts/`、`assets/`（可选）

安装流程：提取到 staging → 校验 frontmatter → 安全扫描 SKILL.md → 安全扫描 scripts/（executable=true）→ 检查冲突 → 移动到目标目录。

### 5.4 支持文件路径强制

`ensure_safe_support_path()` 只允许路径在 `references/`、`templates/`、`scripts/`、`assets/` 下。拒绝绝对路径、`..` 穿越、不允许的顶层目录。

**兜底策略**:
- 历史写入失败 → 记录日志，主操作继续
- `extensions_config.json` 读取失败 → 使用空配置，全部 skill 默认启用
- 安装失败 → 清理 staging 目录，返回错误

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `SkillStorage.__init__(root_path, container_path)` | `harness/skills/storage.py` | 35 | 初始化存储实例 |
| `SkillStorage.get_custom_skill_dir(name)` | `harness/skills/storage.py` | 68 | 返回 `custom/<name>/` 路径 |
| `SkillStorage.get_skill_history_file(name)` | `harness/skills/storage.py` | 77 | 返回 `custom/.history/<name>.jsonl` 路径 |
| `SkillStorage.custom_skill_exists(name)` | `harness/skills/storage.py` | 87 | 检查 custom skill 是否存在 |
| `SkillStorage.public_skill_exists(name)` | `harness/skills/storage.py` | 90 | 检查 public skill 是否存在 |
| `SkillStorage.read_custom_skill(name)` | `harness/skills/storage.py` | 166 | 读取 SKILL.md 全文 |
| `SkillStorage.write_custom_skill(name, relative_path, content)` | `harness/skills/storage.py` | 174 | **原子写入**（tempfile + replace），路径穿越防护 |
| `SkillStorage.delete_custom_skill(name)` | `harness/skills/storage.py` | 203 | `shutil.rmtree` 删除整个目录 |
| `SkillStorage.append_history(name, record)` | `harness/skills/storage.py` | 212 | 追加 JSONL 历史记录 |
| `SkillStorage.read_history(name)` | `harness/skills/storage.py` | 222 | 读取全部历史记录 |
| `SkillStorage.get_skills_root_path()` | `harness/skills/storage.py` | 60 | 返回 host 端 skills 根路径 |
| `SkillStorage.get_container_root()` | `harness/skills/storage.py` | 64 | 返回容器内挂载路径（默认 `/mnt/skills`） |
| `install_skill_from_archive(archive_path, target_root, category, *, force, model_client)` | `harness/skills/installer.py` | 30 | 异步安装 `.skill` ZIP 包 |
| `_extract_archive(archive_path, staging)` | `harness/skills/installer.py` | 132 | ZIP 提取 + 路径穿越检测 + 顶层目录校验 |
| `ensure_safe_support_path(relative_path)` | `harness/skills/installer.py` | 155 | 校验支持文件路径合法性 |
| `ExtensionsConfig.from_file(path=None)` | `harness/config/extensions_config.py` | 65 | 从 `extensions_config.json` 加载配置 |
| `ExtensionsConfig.is_skill_enabled(skill_name, skill_category)` | `harness/config/extensions_config.py` | 105 | 查询 skill 启用状态，默认 `True` |

---

## 6. Cache Invalidation（缓存刷新）

使用 LRU 缓存 skill prompt section，最大 16 个条目。任何 skill 变更（写入、删除、安装、启用/禁用）后必须调用刷新。

**缓存 key**: `sha256("name1:v1;name2:v1;...")[:16]`，基于 skill 名称和版本号。

**兜底策略**: 缓存刷新失败记录日志，下次 `get_system_prompt()` 调用时跳过缓存直接构建。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `refresh_skills_system_prompt_cache()` | `harness/skills/cache.py` | 40 | 替换缓存函数实例，使旧缓存条目被 GC |
| `get_cached_skills_prompt_section(skills_signature, builder)` | `harness/skills/cache.py` | 63 | 命中返回缓存，未命中调用 `builder()` 构建并缓存 |
| `build_skills_signature(skills)` | `harness/skills/cache.py` | 83 | 构建 `"name:version;..."` 格式的签名字符串 |

---

## 7. Injection（注入）

### 7.1 Lead Agent — 渐进式加载

Lead Agent 的 system prompt 中包含 `<skill_system>` XML 块，列出每个 skill 的 `name`、`description`、`category` 和 `location`（容器路径）。LLM 根据用户查询匹配 skill，调用 `file_read` 按需加载完整 SKILL.md。

**白名单控制**: `_available_skill_names()` 从 agent config 读取 `skills` 字段。`None` = 全部启用，`[]` = 无 skill，`["a","b"]` = 白名单。

### 7.2 Subagent — 直接注入

子代理将完整 SKILL.md 内容包装为 `<skill name="...">...</skill>` 的 `SystemMessage`，直接注入消息列表（不经过渐进式加载）。这避免了在子代理有限的 turn 预算中额外消耗一次 `file_read` 往返。

**权限合并**: 子代理实际 skill 列表 = 子代理声明 `skills` ∩ 父代理 `parent_skills`。

**兜底策略**:
- Lead Agent 加载失败 → prompt section 为空字符串
- Subagent 加载失败 → 以无 skill 状态继续执行

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `LeadAgent.get_system_prompt()` | `harness/agents/lead_agent.py` | 470 | 加载 skill → 白名单过滤 → 缓存查找/构建 prompt section → 组装完整 system prompt |
| `LeadAgent._available_skill_names()` | `harness/agents/lead_agent.py` | 463 | 返回 agent config 中的 skill 白名单（`set \| None`） |
| `get_skills_prompt_section(skills, container_base_path)` | `harness/skills/prompt.py` | 8 | 生成 `<skill_system>` XML 块，包含 `<available_skills>` 列表 |
| `apply_prompt_template(agent_name, ..., skills_section)` | `harness/agents/lead_agent.py` | 375 | 组装完整 system prompt 模板 |
| `SubagentExecutor._load_skills()` | `harness/agents/subagent_executor.py` | 274 | 加载子代理 skill，合并父白名单 |
| `SubagentExecutor._merge_skill_allowlists(parent, child)` | `harness/agents/subagent_executor.py` | 251 | 静态方法：父 ∩ 子 skill 交集 |
| `SubagentExecutor._build_skill_messages(skills)` | `harness/agents/subagent_executor.py` | 297 | 将 Skill 对象转为 `<skill>` 包装的 `SystemMessage` 列表 |
| `SubagentExecutor._build_initial_state(task)` | `harness/agents/subagent_executor.py` | 319 | 构建子代理初始状态：skill 消息 → system prompt → HumanMessage(task) |
| `SubagentManager.execute(name, instruction, context, parent_state, *, parent_skills)` | `harness/agents/subagent_manager.py` | 148 | 传递 `skill_storage` 和 `parent_skills` 给 `SubagentExecutor` |

---

## 8. Filtering（过滤）

### 8.1 工具过滤策略

当至少一个已启用 skill 声明了 `allowed-tools`，Lead Agent 和 Subagent 的工具集被限制为所有声明的工具名的**并集**。未声明 `allowed-tools` 的 skill 在有其他 skill 声明时不贡献任何工具。

规则:
- 无 skill → 全允许（legacy）
- 所有 skill 均无 `allowed-tools` 声明 → 全允许（legacy）
- 至少一个 skill 声明 → 工具集 = union of all declared tool names
- `allowed-tools: []` → 该 skill 不添加任何工具

### 8.2 父代理 → 子代理权限合并

```
parent=None, child=None → None (继承全部)
parent=None, child=["a"] → ["a"]
parent=["a","b"], child=None → ["a","b"]
parent=["a"], child=["a","b"] → ["a"] (交集)
parent=[], child=anything → []
```

**兜底策略**: 过滤失败 → 返回未过滤的工具集，继续执行。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `LeadAgent.build_tools()` | `harness/agents/lead_agent.py` | 496 | 白名单过滤 skill → `filter_tools_by_skill_allowed_tools()` |
| `allowed_tool_names_for_skills(skills)` | `harness/skills/tool_policy.py` | 24 | 返回所有声明工具的并集（`set \| None`） |
| `filter_tools_by_skill_allowed_tools(tools, skills)` | `harness/skills/tool_policy.py` | 51 | 将工具列表过滤为仅允许的工具 |
| `SubagentExecutor._filter_tools(all_tools, allowed, disallowed)` | `harness/agents/subagent_executor.py` | 195 | 子代理工具过滤（白名单 + 黑名单） |
| `SubagentExecutor._create_agent(tools)` | `harness/agents/subagent_executor.py` | 330 | 先按 skill 过滤工具，再创建 `create_agent()` |
| `build_lead_tools(manager, *, parent_skills)` | `harness/tools/builtins/lead_tools.py` | 210 | 构建 Lead Agent 工具集，传递 `parent_skills` 给 `task_tool` |
| `task_tool(manager, *, parent_skills)` | `harness/tools/builtins/lead_tools.py` | 93 | 子代理调用工具，传递 `parent_skills` 给 `manager.execute()` |

---

## 9. Execution（执行）

Lead Agent 和 Subagent 在执行时使用过滤后的工具集，按 skill 描述的工作流执行任务。

Lead Agent 使用渐进式加载：LLM 看到 skill 摘要 → 调用 `file_read` 加载完整 SKILL.md → 按 skill 指令执行。

Subagent 直接使用注入的完整 skill 内容执行。

**兜底策略**: skill 文件读取失败 → `file_read` 返回错误，agent 自行决定如何处理。

### 函数

| 函数 | 文件 | 行号 | 说明 |
|------|------|------|------|
| `HarnessService.execute(thread_id, user_id, message, graph, files)` | `harness/main.py` | 498 | Lead Agent 执行入口，SSE 流式输出 |
| `SubagentManager.execute(name, instruction, context, parent_state, *, parent_skills)` | `harness/agents/subagent_manager.py` | 148 | 子代理执行入口 |
| `SubagentExecutor._aexecute(task, result_holder)` | `harness/agents/subagent_executor.py` | 420 | 子代理异步执行核心（astream + 取消检测） |
| `SubagentExecutor.execute(task)` | `harness/agents/subagent_executor.py` | 600 | 子代理同步入口 |

---

## 10. Management（管理层）

### 10.1 REST API（Harness 端口 8001）

| 方法 | 路径 | 函数 | 文件:行号 |
|------|------|------|-----------|
| GET | `/api/skills` | `list_skills()` | `harness/api/routers_skills.py:167` |
| GET | `/api/skills/{name}` | `get_skill()` | `harness/api/routers_skills.py:184` |
| PUT | `/api/skills/{name}` | `toggle_skill()` | `harness/api/routers_skills.py:200` |
| POST | `/api/skills/install` | `install_skill()` | `harness/api/routers_skills.py:225` |
| GET | `/api/skills/custom` | `list_custom_skills()` | `harness/api/routers_skills.py:258` |
| GET | `/api/skills/custom/{name}` | `read_custom_skill()` | `harness/api/routers_skills.py:273` |
| PUT | `/api/skills/custom/{name}` | `write_custom_skill()` | `harness/api/routers_skills.py:285` |
| DELETE | `/api/skills/custom/{name}` | `delete_custom_skill()` | `harness/api/routers_skills.py:327` |
| GET | `/api/skills/custom/{name}/history` | `read_skill_history()` | `harness/api/routers_skills.py:354` |
| POST | `/api/skills/custom/{name}/rollback` | `rollback_skill()` | `harness/api/routers_skills.py:368` |

### 10.2 Agent 自管理工具（`skill_manage`）

| Action | 函数 | 文件:行号 | 说明 |
|--------|------|-----------|------|
| `create` | `_handle_create()` | `harness/tools/skill_manage_tool.py:91` | 创建新 custom skill |
| `edit` | `_handle_edit()` | `harness/tools/skill_manage_tool.py:115` | 替换 SKILL.md |
| `patch` | `_handle_patch()` | `harness/tools/skill_manage_tool.py:128` | 局部修改（append/replace_section） |
| `delete` | `_handle_delete()` | `harness/tools/skill_manage_tool.py:159` | 删除并归档到 history |
| `write_file` | `_handle_write_file()` | `harness/tools/skill_manage_tool.py:170` | 写入支持文件 |
| `remove_file` | `_handle_remove_file()` | `harness/tools/skill_manage_tool.py:192` | 删除支持文件 |
| — | `create_skill_manage_tool(skill_storage, model_client)` | `harness/tools/skill_manage_tool.py:33` | 工具工厂函数 |
| — | `_replace_markdown_section(doc, heading, new_content)` | `harness/tools/skill_manage_tool.py:210` | Markdown 章节替换辅助函数 |

**约束**: 禁止修改 `public/` 下的内置 skill。每次写操作流程: per-skill 锁 → 校验 frontmatter → 安全扫描 → 原子写入 → 追加 JSONL 历史 → 刷新 prompt 缓存。

---

## 完整兜底矩阵

| 失败场景 | 行为 | 相关函数 |
|----------|------|----------|
| SKILL.md 解析失败 | 静默跳过，不影响其他 skill | `parse_skill_file()` 返回 `None` |
| 单个 skill 文件读取失败 | 记录日志并跳过 | `SkillStorage.load_skills()` |
| frontmatter 校验失败 | 拒绝写入/安装，返回 400 | `_validate_skill_frontmatter()` |
| 安全扫描失败/模型异常 | 保守返回 `BLOCK` | `scan_skill_content()` |
| `.skill` 压缩包解压失败 | 清理 staging，返回 500 | `_extract_archive()` |
| skill 根路径不存在 | 自动创建 public/custom 目录 | `HarnessService.initialize()` |
| `extensions_config.json` 读取失败 | 使用默认空配置，全部启用 | `ExtensionsConfig.from_file()` |
| Lead Agent skill 加载失败 | prompt section 为空字符串 | `LeadAgent.get_system_prompt()` |
| Lead Agent tool-policy 过滤失败 | 返回未过滤工具集 | `LeadAgent.build_tools()` |
| Subagent skill 加载失败 | 子代理以无 skill 状态继续 | `SubagentExecutor._load_skills()` |
| 父代理禁止子代理使用某 skill | 子代理 skill 列表与之取交集 | `_merge_skill_allowlists()` |
| 缓存刷新失败 | 记录日志，下次加载时跳过缓存 | `refresh_skills_system_prompt_cache()` |
| 历史写入失败 | 记录日志，主操作继续 | `SkillStorage.append_history()` |
| 路径穿越攻击 | 拒绝并返回 400 / ValueError | `SkillStorage.write_custom_skill()` / `ensure_safe_support_path()` |

---

## 核心数据流

```
extensions_config.json
       │
       ▼
ExtensionsConfig.from_file() ──→ SkillStorage.load_skills()
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
                    ▼                      ▼                      ▼
            LeadAgent             SubagentExecutor          REST API
            .get_system_prompt()   ._load_skills()          /api/skills/*
            .build_tools()         ._build_skill_messages()
            ._available_skill_     ._create_agent()
              names()
                    │                      │
                    ▼                      ▼
            refresh_skills_system_prompt_cache()
                    │
                    ▼
            get_cached_skills_prompt_section()
                    │
                    ▼
            get_skills_prompt_section()
```

---

## 文件索引

### 核心模块 (`harness/skills/`)

| 文件 | 说明 |
|------|------|
| `types.py` | `Skill` 数据类、`SkillCategory` 枚举、`SKILL_MD_FILE` 常量 |
| `parser.py` | YAML frontmatter 解析器 |
| `validation.py` | frontmatter 校验器 |
| `storage.py` | 本地文件系统 SkillStorage（发现、CRUD、历史） |
| `prompt.py` | `<skill_system>` XML 生成 |
| `tool_policy.py` | 工具过滤策略 |
| `security_scanner.py` | LLM 安全扫描器 |
| `cache.py` | LRU 缓存 + 刷新 |
| `installer.py` | `.skill` ZIP 安装器 + 路径强制 |
| `__init__.py` | 公共 API 延迟导入 |

### 集成点

| 文件 | 说明 |
|------|------|
| `harness/agents/lead_agent.py` | Lead Agent skill 注入 + 白名单 + 工具过滤 + 缓存 |
| `harness/agents/subagent_executor.py` | 子代理 skill 加载 + 注入 + 权限合并 + 工具过滤 |
| `harness/agents/subagent_manager.py` | 传递 `skill_storage` 和 `parent_skills` |
| `harness/tools/builtins/lead_tools.py` | `task_tool` 传递 `parent_skills` |
| `harness/tools/skill_manage_tool.py` | Agent 自管理工具 |
| `harness/main.py` | `HarnessService` 初始化 skill 系统 |
| `harness/models.py` | `SubAgentConfig.skills` 字段 |
| `harness/config/extensions_config.py` | skill 启用状态管理 |
| `harness/api/server.py` | skills 路由注册 |
| `harness/api/routers_skills.py` | REST API 端点 |

### 测试文件 (168 tests)

| 文件 | 用例 | 覆盖 |
|------|------|------|
| `tests/test_skills_parser.py` | 25 | 解析器全路径 |
| `tests/test_skills_storage.py` | 27 | 存储 CRUD + 发现 + history |
| `tests/test_skills_integration.py` | 9 | 提示生成 + Storage↔Prompt |
| `tests/test_skills_tool_policy.py` | 13 | 工具过滤策略 |
| `tests/test_skills_real.py` | 16 | 真实 skill 文件端到端 |
| `tests/test_skills_complex.py` | 25 | 复杂 skill + 支持文件 |
| `tests/test_skills_security_scanner.py` | 29 | ALLOW/WARN/BLOCK + 降级 |
| `tests/test_skills_installer.py` | 18 | ZIP 安装 + 路径强制 |
| `tests/test_lead_agent_skills.py` | 17 | 白名单过滤 + 缓存 |
| `tests/test_subagent_skills.py` | 20 | 子代理注入 + 权限合并 |
| `tests/test_skill_manage_tool.py` | 20 | CRUD + patch + 文件管理 |
