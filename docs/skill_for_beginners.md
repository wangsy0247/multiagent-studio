# Skill 模块小白学习文档

> 本文面向刚接触 multiagent-studio 的开发者，用最通俗的语言解释什么是 Skill、怎么写 Skill、以及 Skill 在系统中是怎么运转的。
> 不需要你懂复杂的框架源码，跟着例子一步步来即可。

---

## 一、Skill 是什么？

想象你正在经营一家餐厅：

- **Agent（代理）** 就是餐厅里的厨师，他会做菜，但遇到新菜式可能需要你一步步教。
- **Skill（技能）** 就是一本写好的菜谱。厨师拿到菜谱后，就能按步骤做出菜，不需要你每次都重新口述。

在 multiagent-studio 里，**Skill 是一份写在 `SKILL.md` 文件里的“工作流说明书”**。它告诉 Agent：

- 这个 Skill 适合处理什么任务
- 接到任务后第一步做什么、第二步做什么
- 可以使用哪些工具
- 最终输出什么样的结果

---

## 二、为什么需要 Skill？

### 场景 1：没有 Skill 的时候

你每次让 Agent 做“深度研究”，都要在对话里写一大段：

> “请先用 web_search 搜索 5 个来源，然后用 web_fetch 读取每个网页，最后整理成一份报告……”

很累，而且每次都得重复。

### 场景 2：有了 Skill 之后

你提前写好一个 `deep-research` Skill。之后只需要说：

> “帮我深度研究一下量子计算。”

Agent 会自动加载 `deep-research` Skill，按照里面写好的步骤执行。

### Skill 的好处

| 好处 | 说明 |
|------|------|
| **复用** | 写一次，反复用 |
| **稳定** | 同样的任务，每次执行方式一致 |
| **安全** | 可以限制 Agent 能调用的工具 |
| **清晰** | 复杂流程被封装成可维护的文档 |

---

## 三、你的第一个 Skill

### 3.1 找一个例子看看

multiagent-studio 已经自带了一些 public Skill，打开看看：

```bash
cat skills/public/deep-research/SKILL.md
```

大概长这样：

```markdown
---
name: deep-research
description: Conduct multi-source deep research on a topic and produce a structured report.
license: MIT
allowed-tools:
  - web_search
  - web_fetch
  - file_read
  - file_write
---

# Deep Research

When asked to perform deep research, follow these steps:

1. Clarify the research question. If it is ambiguous, ask the user to narrow it down.
2. Use `web_search` to find at least 5 relevant sources.
3. Use `web_fetch` to read the content of each source.
4. Synthesize the findings into a structured report with:
   - Executive Summary
   - Key Findings
   - Sources
5. Save the report to the workspace using `file_write`.
```

### 3.2 自己写一个最简单的 Skill

我们在 `skills/custom/hello-skill/` 下创建 `SKILL.md`：

```bash
mkdir -p skills/custom/hello-skill
cat > skills/custom/hello-skill/SKILL.md << 'EOF'
---
name: hello-skill
description: Greet the user in a friendly and enthusiastic way.
---

# Hello Skill

When the user says hello, hi, or greets you:

1. Respond warmly.
2. Ask how you can help today.
3. Keep the reply under 50 words.
EOF
```

现在你已经写好了人生中第一个 Skill！

---

## 四、SKILL.md 文件格式详解

每个 `SKILL.md` 都分成两部分：

### 4.1 头部（Frontmatter）

写在两个 `---` 之间，是 YAML 格式：

```yaml
---
name: hello-skill                # skill 名字（必填）
description: Greet the user...   # skill 描述（必填）
license: MIT                     # 许可证（可选）
allowed-tools:                   # 允许使用的工具（可选）
  - web_search
  - file_read
---
```

#### 字段说明

| 字段 | 是否必填 | 规则 |
|------|----------|------|
| `name` | ✅ | 只能用小写字母、数字、连字符 `-`，例如 `my-cool-skill` |
| `description` | ✅ | 一句话描述这个 skill 是做什么的 |
| `license` | ❌ | 许可证，例如 `MIT` |
| `allowed-tools` | ❌ | 字符串列表，限制 skill 能调用的工具 |
| `version` | ❌ | 版本号，例如 `"1.0"` |
| `author` | ❌ | 作者名 |

### 4.2 正文（Body）

头部下面就是普通的 Markdown，写具体的工作流程和指令。

例如：

```markdown
# Hello Skill

When the user says hello, hi, or greets you:

1. Respond warmly.
2. Ask how you can help today.
3. Keep the reply under 50 words.
```

写正文的小技巧：

- 用 `1. 2. 3.` 写步骤，Agent 更容易理解。
- 用代码块展示期望的输出格式。
- 用 `#` 标题分节。
- 明确说“如果 A 就做什么，如果 B 就做什么”。

---

## 五、Skill 的一生（简化版）

一个 Skill 从被创建到被使用，会经历这些阶段：

```text
1. 发现（Discovery）
   系统启动时扫描 skills/public/ 和 skills/custom/ 目录。

2. 解析（Parsing）
   读取每个 SKILL.md，解析头部 YAML 和正文。

3. 验证（Validation）
   检查 name、description 等是否符合规则。

4. 存储（Storage）
   把 Skill 信息保存到内存，启用状态保存在 extensions_config.json。

5. 注入（Injection）
   把 Skill 列表放进 Agent 的“系统提示词”里。

6. 过滤（Filtering）
   根据 Agent 配置决定加载哪些 Skill，并限制可用工具。

7. 执行（Execution）
   Agent 根据任务匹配 Skill，调用 file_read 读取完整内容，然后执行。

8. 管理（Management）
   通过 API 或工具创建、修改、删除、启用、禁用 Skill。
```

你现在只需要知道：**写好 SKILL.md → 放到 skills/custom/ → 系统自动发现并可用**。

---

## 六、动手写一个实用的 Skill

### 目标

写一个 `code-review` Skill，帮用户做代码审查。

### 步骤 1：创建目录和文件

```bash
mkdir -p skills/custom/code-review
cat > skills/custom/code-review/SKILL.md << 'EOF'
---
name: code-review
description: Review code snippets for bugs, style issues, and improvements.
license: MIT
allowed-tools:
  - file_read
  - file_write
---

# Code Review Skill

When the user asks you to review code:

1. Read the code carefully. If it is in a file, use `file_read` to load it.
2. Check for:
   - Bugs or logic errors
   - Performance issues
   - Security risks
   - Code style and readability
   - Missing tests or documentation
3. Produce a structured review with these sections:
   - Summary
   - Critical Issues
   - Suggestions
   - Good Points
4. If the user asks, write the fixed code to a new file using `file_write`.

Keep the tone constructive and specific. Quote problematic lines when possible.
EOF
```

### 步骤 2：重启服务（如果需要）

系统启动时会扫描 Skill。如果你已经在运行中，可以等待自动刷新，或重启服务。

### 步骤 3：测试

在对话框里输入：

> “请帮我 review 这段代码：def add(a, b): return a + b”

Agent 应该会匹配到 `code-review` Skill，并按照你写的步骤给出代码审查。

---

## 七、工具控制：allowed-tools

### 7.1 为什么要限制工具？

假设你写了一个只做“网页搜索”的 Skill，你肯定不希望它去删你的文件。`allowed-tools` 就是用来做这件事的。

### 7.2 示例

```yaml
---
name: web-only-research
description: Research topics using only web tools.
allowed-tools:
  - web_search
  - web_fetch
---
```

这样，加载这个 Skill 的 Agent 就只能使用 `web_search` 和 `web_fetch`。

### 7.3 三种情况

| allowed-tools 写法 | 含义 |
|---------------------|------|
| 不写这个字段 | 不限制，允许所有工具 |
| `allowed-tools: []` | 显式禁止所有工具 |
| `allowed-tools: [a, b]` | 只允许 a 和 b |

### 7.4 多个 Skill 同时加载会怎样？

如果 Agent 同时加载了多个 Skill，且其中任意一个声明了 `allowed-tools`，那么它会取这些声明的**并集**。

例如：

- Skill A：`allowed-tools: [file_read]`
- Skill B：`allowed-tools: [file_write]`

最终 Agent 可用工具 = `file_read` + `file_write`。

---

## 八、启用和禁用 Skill

### 8.1 通过配置文件

编辑 `harness/extensions_config.json`：

```json
{
  "mcpServers": {},
  "skills": {
    "hello-skill": { "enabled": true },
    "code-review": { "enabled": false }
  }
}
```

### 8.2 默认行为

如果某个 Skill 没有在 `extensions_config.json` 里出现，默认是**启用**的。

### 8.3 小贴士

- 禁用某个 Skill 后，它不会出现在 Agent 的可用列表里。
- 你可以保留很多 Skill，但只启用当前需要的，避免 prompt 太长。

---

## 九、Skill 应该放在哪里？

```text
skills/
├── public/          # 内置 Skill，只读，项目自带
│   ├── deep-research/
│   │   └── SKILL.md
│   └── code-reviewer/
│       └── SKILL.md
└── custom/          # 用户自定义 Skill，可编辑
    ├── hello-skill/
    │   └── SKILL.md
    └── code-review/
        ├── SKILL.md
        ├── references/     # 参考资料
        ├── templates/      # 模板文件
        ├── scripts/        # 可执行脚本
        └── assets/         # 图片等静态资源
        └── .history/       # 修改历史（系统自动维护）
```

### 规则

- 每个 Skill 一个目录，目录名建议和 `name` 一致。
- 目录里必须有一个 `SKILL.md`。
- `public/` 下的 Skill 不要乱改，改自己的放 `custom/`。
- 支持文件放在 `{references, templates, scripts, assets}` 子目录里。

---

## 十、常见错误

### 错误 1：name 用了大写字母或空格

❌ 错误：

```yaml
name: My Cool Skill
```

✅ 正确：

```yaml
name: my-cool-skill
```

### 错误 2：缺少 description

❌ 错误：

```yaml
---
name: hello-skill
---
```

✅ 正确：

```yaml
---
name: hello-skill
description: Greet the user.
---
```

### 错误 3：YAML 格式错误

❌ 错误（allowed-tools 缩进不对）：

```yaml
allowed-tools:
- web_search
- file_read
```

✅ 正确：

```yaml
allowed-tools:
  - web_search
  - file_read
```

### 错误 4：Skill 目录里有 SKILL.md 但系统没发现

检查：

- 文件名是否真的是 `SKILL.md`（不是 `skill.md` 或 `Skill.md`）。
- 是否放在了 `skills/public/` 或 `skills/custom/` 下。
- 目录是否被隐藏（以 `.` 开头会被跳过）。

### 错误 5：修改了 public Skill 但没生效

系统可能优先使用缓存。重启服务或等待缓存刷新。

---

## 十一、进阶：自定义 Agent 使用指定 Skill

### 11.1 为 Lead Agent 指定 Skill

在 Agent 配置里加上 `skills` 字段：

```yaml
# agents/my-researcher.yaml
name: my-researcher
model: gpt-4o
skills:
  - deep-research
  - web-only-research
```

这样这个 Agent 只会加载这两个 Skill。

### 11.2 三种写法

| `skills` 值 | 含义 |
|-------------|------|
| 不写 | 加载所有启用的 Skill |
| `skills: []` | 不加载任何 Skill |
| `skills: [a, b]` | 只加载 a 和 b |

### 11.3 为子代理指定 Skill

子代理也可以在配置里写 `skills`：

```python
# 伪代码示例
SubagentConfig(
    name="researcher",
    skills=["deep-research"],
)
```

子代理会把这个 Skill 的内容作为 SystemMessage 注入到自己的对话里。

---

## 十二、最佳实践

### 12.1 Skill 要小而不是大

一个 Skill 只做一件事。不要写“万能 Skill”。

❌ 不好：

```yaml
name: super-agent
description: Do everything for the user.
```

✅ 好：

```yaml
name: sql-review
name: frontend-design
name: meeting-notes
```

### 12.2 description 要具体

Agent 靠 description 判断要不要用这个 Skill。

❌ 不好：

```yaml
description: A useful skill.
```

✅ 好：

```yaml
description: Review Python code for bugs, security issues, and style problems.
```

### 12.3 步骤要清晰

用编号列表写步骤，每步一个动作。

```markdown
1. Read the user's requirements carefully.
2. Ask clarifying questions if anything is ambiguous.
3. Produce a plan with 3-5 steps.
4. Execute the plan one step at a time.
5. Summarize the result.
```

### 12.4 限制工具集

如果 Skill 不需要写文件，就不要给 `file_write` 权限。最小权限原则。

### 12.5 多测试

写好 Skill 后，用几个不同的输入测试，看 Agent 是否按预期执行。如果不行，调整正文描述。

---

## 十三、练习

### 练习 1：天气助手 Skill

写一个 `weather-assistant` Skill，当用户问天气时：

1. 询问具体城市。
2. 调用 `web_search` 查询天气。
3. 用简洁的中文回复温度和天气状况。

### 练习 2：会议纪要 Skill

写一个 `meeting-notes` Skill，当用户给了一段会议录音转录文本时：

1. 提炼议题。
2. 列出决议事项。
3. 列出待办任务和负责人。

### 练习 3：限制工具的 Skill

写一个 `read-only-researcher` Skill，只允许使用 `web_search` 和 `web_fetch`，禁止写文件。

---

## 十四、总结

- **Skill 就是一份说明书**，告诉 Agent 怎么完成特定任务。
- **文件格式**：`SKILL.md`，头部是 YAML，正文是 Markdown。
- **存放位置**：`skills/custom/` 放自己的，`skills/public/` 放内置的。
- **核心字段**：`name`、`description`、`allowed-tools`。
- **使用方式**：系统自动发现，Agent 根据任务匹配并加载。
- **管理**：通过 `extensions_config.json` 启用/禁用，未来可通过 API 管理。

现在你已经可以开始写自己的 Skill 了。记住：**先写一个简单的，跑通了再慢慢加功能。**

---

## 附录：一个完整的 SKILL.md 模板

```markdown
---
name: your-skill-name
description: A clear, specific description of what this skill does.
license: MIT
version: "1.0"
author: your-name
allowed-tools:
  - file_read
  - file_write
  - web_search
---

# Your Skill Title

## When to use

Describe what kind of user request should trigger this skill.

## Steps

1. Step one.
2. Step two.
3. Step three.

## Output format

Show an example of the expected output.

## Notes

Any special instructions, edge cases, or safety reminders.
```

保存为 `skills/custom/your-skill-name/SKILL.md`，然后测试吧！
