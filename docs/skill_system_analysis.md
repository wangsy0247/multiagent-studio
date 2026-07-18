# MultiAgent-Studio Skill 系统现状分析报告

> **分析日期**: 2026-07-17
> **评估基准**: `../skill_module_analysis.md`(2026-07-06)提出的完整功能链 ——
> **发现 → 解析 → 验证 → 存储 → 注入 → 执行 → 过滤 → 管理**,以及横切关注点(缓存、并发安全、沙箱映射、压缩保留)
> **评估对象**: 当前代码库(commit `029bf54` 之后的工作区状态)

---

## 〇、总体结论

功能链 8 个环节**全部有形**,核心链路(发现 → 解析 → 验证 → 存储 → 注入 → 过滤 → 管理)完整度较高;
管理面(LLM 安全扫描、`.skill` 安装器、历史回滚、per-user 私有技能、Team 集成)已**超出参考设计的 MVP 范围**,
达到参考文档 Phase 5 之后的水平。

~~最大的缺口~~参考文档第七章强调的**压缩保留机制已于 2026-07-17 修复**(详见 §9.2):
- SummarizationMiddleware 已实现**技能救援(Skill Rescue)**,4 个技能保留配置参数已被工厂消费,默认工具名已补 `file_read`;
- TodoMiddleware 已实现**上下文丢失提醒注入**与**提前退出拦截**(含提醒上限、per-run 隔离)。

另存在若干 wiring gap(配置已接但生产路径未生效)、API 部署错位、技能内容悬空引用等问题,详见第八章。

**功能链覆盖速览**:

| 环节 | 状态 | 核心位置 |
|------|------|----------|
| 1. 发现 | ✅ 完整 | `harness/skills/storage.py:166-201` |
| 2. 解析 | ✅ 完整 | `harness/skills/parser.py:60-127` |
| 3. 验证 | ✅ 完整 | `harness/skills/validation.py:34-114` |
| 4. 存储 | ✅ 完整 | `harness/skills/storage.py:207-350` |
| 5. 注入 | ✅ 完整(双模式) | `harness/skills/prompt.py:13-57` 等 |
| 6. 执行 | ✅ 完整 | 两个 sandbox provider 只读挂载 `/mnt/skills` |
| 7. 过滤 | ⚠️ 部分(Teammate 缺、实际空转) | `harness/skills/tool_policy.py` |
| 8. 管理 | ⚠️ 大部分(API 有实质问题) | `harness/api/routers_skills.py` 等 |
| 横切:压缩保留 | ✅ 已实现(2026-07-17) | `harness/middleware/summarization.py`、`harness/middleware/todo.py` |
| 横切:沙箱映射 | ✅ 完整 | `local_sandbox_provider.py:88-103` 等 |
| 横切:缓存 | ⚠️ 简化版 | `harness/skills/cache.py` |

---

## 一、发现(Discovery)

**功能链定义**: 遍历技能目录,定位所有 `SKILL.md` 文件。

**当前实现**:`SkillStorage` 扫描两类来源 ——

1. **内置技能目录** `<project_root>/skills/builtin/`:
   `_iter_skill_files()`(`harness/skills/storage.py:166-182`)使用 `os.walk(category_path, followlinks=True)`,
   跳过 `.` 开头的隐藏目录,目录中含 `SKILL.md` 即 yield `(category, category_root, md_path)`。
2. **用户私有技能目录** `{data_root}/users/{user_id}/skills/`:
   `_iter_user_skill_files()`(`storage.py:184-201`)以同样方式遍历 per-user 目录。

**与参考设计的偏差(合理演进)**:
参考设计是 `skills/public/` + `skills/custom/` 双目录;当前已迁移为 **`builtin/` + per-user 私有**布局。
`SkillCategory` 只剩 `BUILTIN` 一个取值(`harness/skills/types.py:14-20`),用户技能靠 `Skill.user_id` 字段区分,
共享 `custom/` 已移除(`storage.py:52` 注释;`get_custom_skill_dir` 在 `user_id=None` 时直接抛 `ValueError`,storage.py:96-108)。

**边界行为**:
- 目录不存在 → 静默跳过,技能列表为空(`HarnessService.initialize()` 会在启动时确保 `skills/` 存在,`main.py:275-301`);
- 嵌套技能目录(`builtin/subdir/nested/SKILL.md`)可正常发现,`relative_path` 记录相对路径;
- `followlinks=True` 配合符号链接 `<skills_root>/my → users/<uid>/skills/` 把用户技能合并进单一视图(`harness/config/paths.py:54-83`)。

---

## 二、解析(Parsing)

**功能链定义**: YAML frontmatter → 结构化 `Skill` 对象。

**当前实现**:`parse_skill_file()`(`harness/skills/parser.py:60-127`)——

1. 用正则 `r"^---\s*\n(.*?)\n---\s*\n"`(`re.DOTALL`)提取 frontmatter(parser.py:67-69);
2. `yaml.safe_load` 解析,结果必须是 dict;
3. 必填校验:`name`、`description` 均为非空字符串,strip 后仍非空(parser.py:86-99);
4. 可选字段:`license`(转字符串 strip,空则 None,parser.py:101-103);
5. `allowed-tools` 经 `parse_allowed_tools()`(parser.py:14-41)解析:
   - 字段省略 → `None`(语义 = 不限制,参与 legacy allow-all);
   - 必须是字符串 list、不允许空字符串元素,否则抛 `ValueError` → 整个技能解析失败返回 `None`;
   - 显式 `[]` → 空列表(语义 = 该技能不贡献任何工具)。
6. 成功时构造 `Skill`(parser.py:106-125),`enabled` 先置 `True`(注释说明实际状态来自 extensions_config)。

**失败处理**: 任何失败(文件不存在、文件名不是 `SKILL.md`、无 frontmatter、YAML 损坏、非 dict、必填缺失、
allowed-tools 畸形、非 UTF-8)→ log + 返回 `None`,**不抛异常**,单技能解析失败不影响其他技能加载。

**数据模型**(`types.py:23-36`):
`name / description / license / skill_dir / skill_file / relative_path / category / allowed_tools / enabled / user_id`。
容器路径辅助:`get_container_path()`(types.py:44-58)→ 用户技能 `/mnt/skills/my/<rel>`,内置 `/mnt/skills/builtin/<rel>`;
`get_container_file_path()` 拼 `/SKILL.md`(types.py:60-62)。

---

## 三、验证(Validation)

**功能链定义**: 名称约定 / 字段白名单 / 安全检查。

**当前实现**: 两个层面 ——

**1. frontmatter 全量校验** `_validate_skill_frontmatter()`(`harness/skills/validation.py:34-114`),用于所有**写入路径**:
- **字段白名单** `ALLOWED_FRONTMATTER_PROPERTIES`(validation.py:15-26):
  `name / description / license / allowed-tools / metadata / compatibility / version / author`,**未知键直接拒绝**(line 63-69);
- 名称正则 `^[a-z0-9]+(?:-[a-z0-9]+)*$`(line 29),长度 ≤ 64(line 30, 91);
- 描述:禁止 `<` `>`(防 `<skill_system>` XML 注入,line 100),长度 ≤ 1024(line 31, 102);
- `allowed-tools` 复用 `parse_allowed_tools` 校验(line 111-114)。

**2. 存储层名称校验** `SkillStorage.validate_skill_name()`(`storage.py:72-82`):
同一正则 + 长度限制在 storage 层重复实现;正则本身排除 `..`、斜杠等字符,**间接提供路径遍历防护**。

**纵深防御补充**:
- `write_custom_skill()` 写入前 `resolve()` + `is_relative_to()` 检查,目标必须在技能目录内(`storage.py:294-301`);
- `ensure_safe_support_path()`(`harness/skills/installer.py:206-249`)把支持文件限制在
  `references/ templates/ scripts/ assets/` 白名单内,拒绝绝对路径与 `..`,并用 fake root 双重 resolve 校验。

**已知问题**: 内置技能 `system-architecture-review` 使用了非标准字段 `compatibility.tools`
(`skills/builtin/system-architecture-review/SKILL.md:7-9`),虽在白名单内可通过校验,但**解析器不消费该字段** ——
若作者意图是限制工具,实际未生效(测试已按 `allowed_tools is None` 固化此行为,`harness/tests/test_skills_real.py:85-95`)。

---

## 四、存储(Storage)

**功能链定义**: 内存中的 Skill 对象列表 + 启用状态合并 + CRUD。

**当前实现**:`SkillStorage`(`harness/skills/storage.py`)——

**加载与合并** `load_skills()`(storage.py:207-265):
1. 先扫内置、再扫用户技能,**同名时用户技能覆盖内置**(后写覆盖,line 244-245);
2. **每次调用都重新扫描磁盘**并重读 `extensions_config.json`(注释见 storage.py:217-218),
   用 `ExtensionsConfig.is_skill_enabled(name, category)` 合并启用状态 —— **未配置默认启用**
   (`harness/config/extensions_config.py:243-253`);
3. `enabled_only=True` 可选过滤;按 name 排序返回。

**CRUD**(仅针对 per-user 目录):
- `read_custom_skill()`(storage.py:271);
- `write_custom_skill()`(storage.py:281-312):**tempfile + `replace` 原子写** + 路径逃逸检查;
- `delete_custom_skill()`(storage.py:314-323):`shutil.rmtree`;
- 历史:`append_history()` / `read_history()`(storage.py:325-350),JSONL 格式,
  存放在 `{uid}/skills/.history/<name>.jsonl`。

**启用状态的读写分裂(值得注意)**:
`extensions_config.py` **只有读**(`from_file`,line 158-207;容错:文件缺失/JSON 损坏 → 默认空配置;
支持 `$VAR` 环境变量递归解析),**没有任何 save/write 方法**。唯一的写入方是 API 层
`_write_extensions_config()`(`harness/api/routers_skills.py:72-87`,tempfile + replace 原子写),
由 toggle 端点(routers_skills.py:234-259)调用。

**多进程一致性**: 读侧每次 `from_file()` 重读文件(不走缓存单例),外部改动即时生效;
写侧原子 rename,读-写竞争下不会读到半截 JSON。

---

## 五、注入(Injection)

**功能链定义**: XML `<skill_system>` 块注入系统提示。

**当前实现**: **双模式注入**,生成函数统一为
`get_skills_prompt_section(skills, container_base_path="/mnt/skills")`(`harness/skills/prompt.py:13-57`):
- 空列表 → 返回 `""`(块完全省略);
- 否则生成 `<skill_system>` XML:渐进式加载四步说明(匹配 → `file_read` `<location>` → 理解 → 按需加载同目录资源)
  + `**Skills Root:** /mnt/skills` + `<available_skills>` 列表;
- 每条技能:`<skill><name>/<description> [mine|built-in]/<location>`(prompt.py:31-38),
  location 形如 `/mnt/skills/builtin/<rel>/SKILL.md` 或 `/mnt/skills/my/<rel>/SKILL.md`;
- 描述中的 `<`/`>` 已在验证层禁止,XML 注入在源头拦截。

**模式 A — Lead Agent(渐进加载)**:
- 模板占位符 `{skills_section}` 位于 `SYSTEM_PROMPT_TEMPLATE`(`harness/agents/lead_agent.py:367`,
  在 `working_directory_section` 与 `memory_tool_section` 之间,与参考设计 §9.4 一致);
- `get_system_prompt()`(lead_agent.py:551-594)每次调用 `skill_storage.load_skills(enabled_only=True, user_id=...)`(lead_agent.py:560);
- prompt section 字符串有 **LRU 缓存**(`harness/skills/cache.py:81-116`,maxsize=16,key 为 `name:version` 签名),
  技能变更时由 `refresh_skills_system_prompt_cache()` 显式失效
  (调用点:`routers_skills.py:93-95`、`skill_manage_tool.py:389-391`)。

**模式 B — Teammate(Team 模式,渐进加载)**:
`_build_skills_section()`(`harness/team/teammate_agent.py:224-263`)与 Lead 同款,
并支持 agent 级白名单 `_get_skill_whitelist()`(teammate_agent.py:204-222):
`None` = 全部启用技能;空集 = 无技能;`{"a","b"}` = 仅指定技能。取自 `EffectiveConfig.skills`,回退旧 `AgentConfig.skills`。

**模式 C — Subagent(直接注入全文)**:
`_build_skill_messages()`(`harness/agents/subagent_executor.py:355-385`)**直接读取 SKILL.md 全文**
包成 `<skill name="...">` SystemMessage,放在初始 state 消息最前(`_build_initial_state`,subagent_executor.py:459-465)。
不走渐进加载,理由是子代理 turn 预算有限、没有余量做二次读取。
白名单经父子**交集**合并:`_merge_skill_allowlists()`(subagent_executor.py:303-323),
child 侧来自 `SubAgentConfig.skills`(`harness/models.py:71`)。

**⚠️ 注入链路的关键缺陷 —— graph 缓存固化**:
system prompt 在 graph 编译时(`main.py:461` 调 `get_system_prompt()`)固化进编译后的 graph,
缓存于 `_graph_cache[(user_id, agent_name)]`(`main.py:179, 391-399`),**该缓存仅 shutdown 时清空**(main.py:372)。
因此运行期创建/禁用技能后,LRU 失效只影响**下一次 graph 编译** —— 已编译 graph 的会话不会看到技能变更。

---

## 六、执行(Execution)

**功能链定义**: LLM 渐进式加载 SKILL.md —— 实际读取行为发生在沙箱内。

**当前实现**:
- 容器路径常量 `VIRTUAL_SKILLS_PATH = "/mnt/skills"`(`harness/config/paths.py:25`);
- **LocalSandboxProvider**(`harness/services/local_sandbox_provider.py:88-103`):
  追加 `PathMapping(container_path="/mnt/skills", local_path=skills_root, read_only=True)`;
  只读由 `write_file` 强制执行(命中 read_only mapping 抛 `OSError`,:245-249);
  `sanitize_output` 反向把宿主路径掩码回 `/mnt/skills`(:177-181);
- **OpenSandboxProvider**(`harness/services/open_sandbox_provider.py:264-280`):
  Docker 卷 `{base_name}-skills` 挂载到 `/mnt/skills`,`read_only=True`;
  支持 `HARNESS_HOST_SKILLS_PATH` 环境变量做 Docker-in-Docker 路径转换(paths.py:224-230);
  项目 `skills/` 先同步镜像到 `{data_root}/skills`(`main.py:275-293`,`_sync_skills_to_data_root()`),
  因 OpenSandbox 只允许 bind-mount data_root 前缀;
- **用户技能合并**:`ensure_user_skills_symlink()`(paths.py:54-83)创建
  `<skills_root>/my → users/<uid>/skills/` 符号链接,规避 Docker overlay2 嵌套只读挂载 bug;
- **工具侧**:`_normalize_virtual_path()`(`harness/tools/sandbox_tools.py:47-70`)把 `/mnt/skills`
  列为合法虚拟命名空间直接放行,其他宿主绝对路径一律拒绝;`file_read` / `list_files` / `glob_tool` / `grep_tool` 均经此归一化。

**写入边界**: `/mnt/skills` 挂载只读,LLM 无法通过 `file_write` 改技能;
修改只能走 `skill_manage` 工具(写 per-user 目录,再经 symlink 暴露回 `/mnt/skills/my/`)。

---

## 七、过滤(Filtering)

**功能链定义**: 按技能声明的 `allowed-tools` 过滤工具列表。

**当前实现**:`harness/skills/tool_policy.py` ——
- `allowed_tool_names_for_skills()`(tool_policy.py:24-48):取所有声明了 `allowed-tools` 技能的工具名**并集**;
  **全部未声明 → 返回 `None`(legacy allow-all)**;一旦有任一技能显式声明,未声明的技能不贡献任何工具;
  显式空 list → 只记 info 日志;
- `filter_tools_by_skill_allowed_tools()`(tool_policy.py:51-63):泛型(`ToolT: NamedTool` protocol),
  `allowed is None` 原样返回,否则按 `tool.name in allowed` 过滤。

**集成点(2/3)**:
| Agent 类型 | 是否过滤 | 位置 |
|---|---|---|
| LeadAgent | ✅ | `lead_agent.py:651-677`(先 per-agent 白名单过滤技能,再 tool_policy 过滤工具) |
| Subagent | ✅ | `subagent_executor.py:422-432`(另有基础过滤 `_filter_tools()` 按 config.tools/disallowed_tools,:194-219) |
| Teammate | ❌ | team/ 目录下无 `filter_tools_by_skill_allowed_tools` 调用 |

**⚠️ 过滤链实际空转**: 6 个内置技能**全部没有 `allowed-tools` 字段**
(`harness/tests/test_skills_real.py:85-95` 已按此断言),`allowed_tool_names_for_skills` 恒返回 `None`,
工具过滤永远走 legacy allow-all 分支。机制正确但当前无实际约束效果。

**per-agent 技能白名单的消费缺口**:
- `AgentConfig.skills` 字段定义于 `harness/config/agents_config.py:108`(另有 `config_models.py:113, 156` 平行定义);
- **单 agent 路径不生效**:`main.py:446-454` 构造 `LeadAgent` 时**未传 `agent_config=`**,
  `LeadAgent._available_skill_names()`(lead_agent.py:538-549)在生产路径恒得 `None`(只有测试覆盖该路径);
- **Team 路径生效**:`teammate_agent.py:204-263` 消费 `EffectiveConfig.skills`;
  `AgentCard.skills`(`harness/team/agent_card.py:53, 225, 235`)用于任务认领评分(+30/个,teammate_agent.py:923-955),
  但 `format_cards_for_prompt()`(:274-296)**不渲染 skills**,prompt 内能力发现只靠 tools + description。

---

## 八、管理(Management)

**功能链定义**: CRUD API + `.skill` 安装 + 启用/禁用。

### 8.1 REST API(`harness/api/routers_skills.py`,前缀 `/api/skills`,挂在 harness:8001)

| 方法 | 路径 | 功能 | 位置 |
|---|---|---|---|
| GET | `/api/skills` | 列出全部技能(`enabled_only` 过滤) | :197 |
| GET | `/api/skills/{name}` | 单技能详情(含支持文件检测) | :215 |
| PUT | `/api/skills/{name}` | 启用/禁用 → 原子写 `extensions_config.json` | :234 |
| POST | `/api/skills/install` | 从 `.skill`/`.zip` 归档安装 | :268 |
| GET | `/api/skills/custom` | 列出用户私有技能 | :311 |
| GET | `/api/skills/custom/{name}` | 读私有技能全文 | :330 |
| PUT | `/api/skills/custom/{name}` | 创建/覆盖私有技能(frontmatter 校验) | :349 |
| DELETE | `/api/skills/custom/{name}` | 删除(先归档到 history) | :411 |
| GET | `/api/skills/custom/{name}/history` | 读 JSONL 历史 | :448 |
| POST | `/api/skills/custom/{name}/rollback` | 回滚到历史版本 | :468 |

注册:`harness/api/server.py:104-108`(`create_app()` 中 `include_router`)。

**⚠️ 三个实质问题**:
1. **路由遮蔽**:`GET /{name}`(:215)注册在 `GET /custom`(:311)**之前**,FastAPI 按注册顺序匹配,
   `GET /api/skills/custom` 会先命中 `/{name}`(name="custom"),`list_custom_skills` **实际不可达**;
2. **安装端点形同虚设**:`POST /install` 在 REST 上下文 `model_client=None`(routers_skills.py:293),
   而安全扫描器 default-deny → **永远拒绝安装**(代码注释自认 "write blocked");
3. **部署错位**:nginx 把 `/api/` 全部转发到 app:8000(`nginx/nginx.conf:50-59`),
   而 skills 路由在 harness:8001 —— 经 nginx 访问 `/api/skills` 会 **404**;
   文件头 docstring(:3)写的 "App service (port 8000)" 与实际注册位置不符。
4. 另:`PUT /custom/{name}` 只做 frontmatter 校验,**未走安全扫描**(与 docstring 声称不符)。

### 8.2 LLM 自管理工具 `skill_manage`(`harness/tools/skill_manage_tool.py`)

- **操作集**(:39-46):`create / edit / patch(append|replace_section) / delete / write_file / remove_file`;
  **无 list/enable/disable**(启用状态归 API 层管);
- **写管道**(:7-14):frontmatter 验证 → **LLM 安全扫描**(agent 侧传入真实 model_client,:65-78;
  无 client 时所有写操作被拒绝)→ 原子写 → JSONL history → 刷新 prompt 缓存(:386-393);
- **目标目录**:全部写 per-user 目录 `{data_root}/users/{uid}/skills/<name>/`;
  builtin 只读由命名空间隔离保证(custom 操作只查 per-user 目录);
- **注册**:per-user 装配(`main.py:435-443`,`set_skill_user_id` ContextVar + 注册到 `"skills"` 工具组);
  `config.yaml:33` 的 `lead_agent.tools` 含 `- skills` → Lead 默认持有;
  **Team 默认 `tool_groups` 不含 `"skills"`**(`harness/config/defaults.py:13`)→ 团队成员默认无此工具。

### 8.3 安装器(`harness/skills/installer.py`)

`install_skill_from_archive()`(:31-165):staging(mkdtemp)→ 解包 → SKILL.md 存在性 + 解析 + 全量校验
→ LLM 扫描 SKILL.md → 逐文件扫描 `scripts/`(executable 模式)→ 重名检查(`force` 覆盖)→ `shutil.move` → 失败清理。

`_extract_archive()`(:168-203)防护:
- 路径遍历:逐成员 `resolve()` 后检查 staging 前缀(:185-189);
- 顶层白名单 `{"SKILL.md","references","templates","scripts","assets"}`(:25, 191-197);
- **⚠️ 无 ZIP 炸弹防护**:不检查解压后总大小、成员数、压缩比,`extractall` 直接全量解出。

### 8.4 安全扫描器(`harness/skills/security_scanner.py`)

**纯 LLM 审计**(无规则/正则兜底,参考设计列为 Phase 2 可跳过项,这里已实现):
- `_SCAN_SYSTEM_PROMPT`(:24-50):7 类威胁(命令注入/数据外泄/提权/恶意载荷/路径遍历/社工/资源滥用),
  要求输出 `ALLOW|WARN|BLOCK + 理由`;
- `scan_skill_content()`(async,:53-138):内容截断 8000 字符;**default-deny** ——
  `model_client=None`、调用异常、输出不可解析 → 全部 `BLOCK`;`executable=True` 时 WARN 升级为 BLOCK;
- 另有 `scan_skill_content_sync` 同步包装(:205-239,注释自认同 event loop 内可能死锁)。

### 8.5 启用/禁用持久化链路

```
PUT /api/skills/{name} {enabled: bool}              (routers_skills.py:234)
  → skills_cfg[name] = {"enabled": ...}             (:257-259)
  → _write_extensions_config() 原子写(tempfile+replace) (:72-87)
  → refresh_skills_system_prompt_cache()            (:90-97)
读侧: load_skills() 每次 from_file() 重读 → is_skill_enabled() 默认 True
```

当前 `harness/extensions_config.json:37` 的 `"skills": {}` 即"从未 toggle 过,全部默认启用"。

---

## 九、横切关注点

### 9.1 缓存架构(简化版,非参考设计架构)

- 只有 **prompt section 字符串的 LRU 缓存**(`cache.py:22, 60-74`,maxsize=16),key 是
  `build_skills_signature()`("name:version;…",cache.py:103-116);
- 失效方式:**整体替换 cached 函数对象**(`refresh_skills_system_prompt_cache`,:41-57);
- **不是**"热路径无阻塞读 + 后台刷新线程 + 版本号"架构(参考设计 §6):无 asyncio 后台任务、无代际计数器;
- `Skill.version` 属性不存在,签名里 `getattr(s, "version", "0")` 恒为 `"0"`(cache.py:114)——
  同名技能内容变更若不触发显式失效,会命中旧缓存;
- 更上游的 graph 缓存固化问题见 §五(注入)。

### 9.2 压缩保留(✅ 已实现 —— 2026-07-17 修复)

参考文档第七章要求的两项机制均已实现(移植自 DeerFlow,适配 HarnessState/TodoItem):

**(a) 技能救援(Skill Rescue)**(`harness/middleware/summarization.py`):
- 重写 `before_model`/`abefore_model` 为完整流程入口(原 `_maybe_summarize` 在 LangChain 1.3.10 下是**死代码** ——
  父类入口为 `before_model`,从不调用该辅助方法;此修复同时复活了 `memory_flush_hook` 与
  `_preserve_dynamic_context_reminders`);
- 新增 `_partition_with_skill_rescue()` / `_find_skill_bundles()` / `_select_bundles_to_rescue()` /
  `_is_skill_tool_call()` / `_tool_call_path()` / `_clone_ai_message()`(后者移植自 DeerFlow
  `tool_call_metadata`,同步 raw tool_calls 与 finish_reason);
- 救援语义:识别读取 `/mnt/skills/**` 的 AIMessage+ToolMessage 对,按"最新优先 + skill_key 去重 +
  count/token 双层预算"保留,同一 AI 消息中技能与非技能 tool_calls 会被拆分;
- **僵尸配置已消费**:`create_summarization_middleware()` 传入全部 4 个参数;
- **工具名错位已修正**:`skill_file_read_tool_names` 默认 `["file_read","read_file","read","view","cat"]`
  (`summarization_config.py:44-46`)。

**(b) Todo 上下文丢失恢复**(`harness/middleware/todo.py`):
- `abefore_model`:todos 存在但 `write_todos` 已滚出上下文 → 注入 `todo_reminder` HumanMessage
  (`hide_from_ui`,已注入则不重复);
- `aafter_model`(`@hook_config(can_jump_to=["model"])`):干净退出 + 有未完成 todo → 排队完成提醒并
  `{"jump_to": "model"}` 强制继续,上限 `_MAX_COMPLETION_REMINDERS=2` 防死循环;全终态仍置
  `plan_mode_exit`(原行为保留);
- `awrap_model_call`:提醒经 `request.override` 注入下一次模型请求,**不持久化**进消息历史;
- per-run 簿记:`(thread_id, run_id)` 键控 + 锁 + LRU 清理,`abefore_agent`/`aafter_agent` 负责跨 run 清理。

**测试**:`harness/tests/test_summarization_skill_rescue.py`(16 例)与
`harness/tests/test_todo_context_loss.py`(16 例)全部通过。

### 9.3 并发安全

| 场景 | 现状 |
|---|---|
| 技能文件写 | ✅ 原子写(tempfile + `replace`,storage.py:304-312) |
| extensions_config 写 | ✅ 原子写(routers_skills.py:79-87) |
| JSONL 历史追加 | ⚠️ 非原子,但低开销低风险 |
| 读-写竞争 | ✅ 读侧每次重读文件,写侧 rename,不会读到半截内容 |
| 缓存失效竞争 | ⚠️ 整体替换函数对象,依赖 GIL 引用赋值原子性;无版本号防 ABA |
| ZIP 炸弹 | ❌ 无防护(见 §8.3) |

### 9.4 测试覆盖(良好,11 个测试文件)

| 环节 | 测试文件 |
|---|---|
| 解析 | `harness/tests/test_skills_parser.py` |
| 存储(含原子写/穿越防护/history/extensions 集成) | `harness/tests/test_skills_storage.py` |
| 过滤 | `harness/tests/test_skills_tool_policy.py` |
| 注入 | `harness/tests/test_skills_integration.py` |
| 安全扫描 | `harness/tests/test_skills_security_scanner.py` |
| 安装 | `harness/tests/test_skills_installer.py` |
| 真实技能回归 | `harness/tests/test_skills_real.py` |
| 复杂技能(支持文件) | `harness/tests/test_skills_complex.py` |
| Lead 集成(含 prompt 缓存/白名单) | `harness/tests/test_lead_agent_skills.py` |
| Subagent 集成(含 allowlist 交集) | `harness/tests/test_subagent_skills.py` |
| 管理工具 | `harness/tests/test_skill_manage_tool.py` |

缺口:压缩保留、Teammate 集成、REST 端点本身无测试;`app/tests/` 零技能测试(技能系统完全在 harness 侧)。

---

## 十、现有技能清单(`skills/builtin/`,共 6 个)

| 技能 | 描述摘要 | allowed-tools | 支持文件 |
|---|---|---|---|
| `code-reviewer` | 系统化代码审查,输出分级 findings | 无 | ⚠️ 正文引用 `references/checklist.md`、`references/python_patterns.md` —— **文件不存在(悬空引用)** |
| `deep-research` | 多源深度研究 → 带引用报告 | 无 | ⚠️ 引用 `references/search_strategies.md`、`templates/report_template.md` —— **悬空** |
| `deployment-checklist` | 生产部署安全检查(含金丝雀/回滚) | 无 | `references/rollback_procedures.md`、`scripts/preflight_check.sh` ✅ |
| `greeting-responder` | 多语言问候应答 | 无 | 无 |
| `my-workflow` | 个人日常工作流 | 无 | ⚠️ 引用 `templates/standup_template.md` —— **悬空** |
| `system-architecture-review` | 架构评审(7 维度评分矩阵) | 无(有非标准 `compatibility.tools`,解析器不消费) | `references/review_checklist.md`、`references/common_anti_patterns.md`、`templates/architecture_report.md` ✅ |

**仓库卫生(布局迁移中间态)**:
- 磁盘上只有 `skills/builtin/`(整个目录 git 未跟踪);`skills/public`、`skills/custom` 在 git 索引中处于已删除未提交状态;
- `.gitignore` 无任何 skills 条目;
- 文档漂移:`docs/skill_lifecycle.md:13` 仍按旧 `public/custom` 布局描述(该文档其余函数索引仍高度准确)。

**前端**: 无技能管理面板,前端零调用 `/api/skills`;
唯一展示是项目页 Agent 卡片上的只读技能徽章(`frontend/src/app/(dashboard)/projects/[id]/page.tsx:534-542`)。
`AgentCard.skills` 等类型声明存在于 `frontend/src/lib/types.ts:306,340,357`。

**app 层**: 无任何技能专属 API/DB 模型;仅 `app/api/agents.py:87,175` 透传 agent 配置的 `skills: list[str]` 字段。

---

## 十一、缺口汇总与建议优先级

| # | 优先级 | 问题 | 位置 |
|---|--------|------|------|
| 1 | P1 | system prompt 固化进 `_graph_cache`,运行期技能变更对已编译 graph 不生效 | `main.py:391-399, 461` |
| 2 | P1 | 单 agent 路径 `AgentConfig.skills` 白名单不生效(未传 `agent_config`) | `main.py:446-454` |
| 3 | P1 | API 路由遮蔽(`/custom` 不可达)、`/install` 永远拒绝、nginx 部署错位 | `routers_skills.py`、`nginx.conf:50-59` |
| 4 | P1 | `execute_async()` 不传 `skill_storage` → 后台子代理无技能;子代理 `_load_skills()` 不传 `user_id` → 看不到用户私有技能 | `subagent_manager.py:294-303`、`subagent_executor.py:325-353` |
| 5 | P2 | Teammate 无 `allowed-tools` 工具过滤;6 个内置技能全部无 `allowed-tools` → 过滤链空转;`compatibility.tools` 语义待定 | `teammate_agent.py`、`skills/builtin/` |
| 6 | P2 | 3 个 SKILL.md 悬空引用(references/templates 文件不存在) | `skills/builtin/` |
| 7 | P3 | 布局迁移未提交、`.gitignore` 无条目、`skill_lifecycle.md` 文档漂移、前端无管理面板 | 仓库根、`docs/`、`frontend/` |

**建议路线**:
1. ~~P0 —— 技能救援 + Todo 提醒/拦截~~(**已完成**,2026-07-17,见 §9.2);
2. **P1** —— 修 wiring gap(main.py 传 `agent_config`、`execute_async` 传 `skill_storage`)、修 API(路由顺序、安装扫描策略、nginx/代理)、评估 graph 缓存失效策略;
3. **P2** —— 补齐技能内容(修悬空引用、明确 `allowed-tools`/`compatibility.tools` 语义)、Teammate 工具过滤;
4. **P3** —— 仓库与文档收尾、前端管理面板。

---

## 附录:关键文件索引

| 类别 | 文件 |
|---|---|
| 核心模块 | `harness/skills/{types,parser,validation,storage,cache,prompt,tool_policy,security_scanner,installer}.py` |
| 配置 | `harness/config/{extensions_config,paths,agents_config,config_models,summarization_config}.py`、`harness/extensions_config.json`、`harness/config.yaml` |
| Agent 集成 | `harness/agents/{lead_agent,subagent_manager,subagent_executor}.py`、`harness/team/{teammate_agent,orchestrator,agent_card,project_lead_agent}.py` |
| 中间件 | `harness/middleware/{summarization,todo}.py` |
| 沙箱 | `harness/services/{local_sandbox_provider,open_sandbox_provider}.py`、`harness/tools/sandbox_tools.py` |
| 管理面 | `harness/api/routers_skills.py`、`harness/tools/skill_manage_tool.py` |
| 技能内容 | `skills/builtin/`(6 个技能) |
| 测试 | `harness/tests/test_skills_*.py` 等 11 个文件 |
| 文档 | `docs/skill_for_beginners.md`(用户教程)、`docs/skill_lifecycle.md`(生命周期设计,部分过时) |
