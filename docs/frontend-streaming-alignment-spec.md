# 前端流式输出与内容折叠对齐 DeerFlow —— 修改方案 Spec

> 日期：2026-08-03
> 目标：将 multiagent-studio 前端的流式输出与内容折叠体验对齐 DeerFlow（`deer-flow-main/deer-flow-main`）。
> 本文档基于对两个项目的代码分析（关键文件与行号见文末附录）。

> **实施状态（2026-08-03 更新）**：Phase 1 / 2 / 3 / 5 / 6 / 7 已全部实施并验证通过。
> 验证基线：`app` 106 测试全绿（新增 27 个）；`harness` 639 passed / 72 failed（72 个为 HEAD 既有失败，逐行比对零新增）；前端 `tsc --noEmit` 与 `npm run build` 通过。
> Phase 4（Platform 协议迁移）按计划不实施。
> 实施中的关键决策：Phase 3 的事件泵为独立后台任务（刷新后仍有实时事件源，落库对刷新免疫）；Phase 5 复用 `tool_call` 事件识别 `present_files` 未新增事件类型；Phase 7b 补上了 `view_image` 工具（middleware 早已存在但无配套工具）并删除了 prompt 中"配套 .md"的断链承诺。

---

## 1. 现状 vs DeerFlow 差距总结

### 1.1 流式输出

| 维度 | 本项目现状 | DeerFlow 做法 | 差距评级 |
|---|---|---|---|
| 传输 | 自研 fetch POST + ReadableStream 解析 `data: {json}` 行 | LangGraph SDK `useStream`（LangGraph Platform SSE 协议：`event:` + `data:` + `id:`） | 大（协议级） |
| Delta 合并 | 自研 `_streamingMessageId` + `_appendToMessage` 按气泡拼接，tool_call 为气泡边界 | SDK `MessageTupleManager` 按 `message.id` 做 chunk concat | 中 |
| 渲染节流 | **无**。每个 token 都 `set()` → 全量重渲染 + ReactMarkdown 整段重解析 | `useStream(throttle:true)` 按 macrotask 合并 + `useDeferredValue` 降级 | 高（性能痛点） |
| 打字机效果 | 无；仅底部三点 "AI 正在思考..." | `useSmoothStreamingContent`：rAF 补帧，300ms 窗口内每帧 ≥8 字符渐进揭示大块文本 | 中（体验） |
| Markdown 渲染 | 流式/完成均用完整 ReactMarkdown + 代码高亮 | Streamdown，`parseIncompleteMarkdown` 容忍未闭合标签；流式期间代码块不高亮，结束后才全量渲染 | 中 |
| 断线续传 | 无事件序号；刷新后退化为 5s 轮询 + 最终整体拉历史，期间无增量输出 | SSE 事件 `id` + `Last-Event-ID` + `reconnectOnMount`，可断点续流；replay gap 有 `gap` 事件兜底 | 高 |
| 停止生成 | 本地立即 abort + 尽力通知后端 | SDK `thread.stop()` → `runs/{id}/cancel`，409 竞态静默处理 | 小 |

### 1.2 内容折叠

| 维度 | 本项目现状 | DeerFlow 做法 | 差距评级 |
|---|---|---|---|
| 过程消息折叠 | `ProcessGroup` 把整组过程消息折叠为 "执行过程 · N 步"；流式中最新组展开，结束自动收起；尊重用户手动切换 | `MessageGroup` "只展示最新"：最后一个 tool call 常驻展示（FlipDisplay），之前步骤折叠成 "N more steps" 按钮；最后一段 thinking 单独折叠按钮 | 中（交互哲学不同） |
| 思考块（Reasoning） | `ThinkingCard` 默认展开，**结束后不自动收起**；历史加载也默认展开（与 ProcessGroup 默认收起不一致） | `Reasoning` 流式时展开 + Shimmer "Thinking..." + 秒表；**流式结束 1s 后自动收起一次**（`hasAutoClosed` 防重复干预），完成后显示 "Thought for N seconds" | 高（明确要对齐的行为） |
| 折叠基础组件 | 手写 `useState` + 条件渲染，无统一动画 | shadcn/ui `Collapsible`（Radix）+ `useControllableState` + tailwindcss-animate 滑入动画 | 低 |
| 子代理展示 | SubAgentCard 点击打开右侧详情面板（subConversations 懒加载） | SubtaskCard 就地折叠卡（默认收起），展开时才回填历史步骤 | 低（各有取舍，可不改） |

---

## 2. 对齐策略：分四个阶段，按 ROI 排序

**重要前提**：DeerFlow 前端能大幅"偷懒"是因为其后端实现了完整的 LangGraph Platform API（threads/runs/stream、事件 id、状态持久化），前端直接复用 SDK。本项目 harness 是自建 FastAPI + 自研 SSE 协议，**完整迁移到 langgraph-sdk 需要后端实现整套 Platform API 表面，工作量巨大且风险高**。因此方案采用"前端体验与交互行为对齐优先，协议对齐可选"的渐进路线：

- **Phase 1（纯前端，高收益低风险）**：渲染节流 + 平滑打字机 + Reasoning 自动收起 + 流式渲染分级。
- **Phase 2（纯前端，中收益）**：ProcessGroup 改为"只展示最新"模式；折叠组件统一化。
- **Phase 3（前后端配合）**：SSE 事件序号 + 断线续传，取代 5s 轮询。
- **Phase 4（可选，大改造）**：协议级对齐 LangGraph Platform + 迁移 `useStream`。仅在确有需求（多端复用、长任务可靠续传）时立项。

---

## 3. Phase 1：流式渲染性能与打字机体验（纯前端）

### 3.1 渲染节流

**问题**：`chat-store.ts` 的 `_appendToMessage` 每个 token chunk 都 `set()` 一次，`messages.map()` 复制数组，正在生长的消息每条都触发 ReactMarkdown 全量重解析。

**方案**：在 store 与渲染之间加一层节流缓冲。

- 修改 `frontend/src/lib/chat-store.ts`：
  - 新增内部缓冲 `_pendingAppends: Map<string, string>`（messageId → 待拼接文本），`handleSSEEvent` 的 `message`/`thinking` 分支只写缓冲，不立即 `set()`。
  - 用 `requestAnimationFrame`（或 50ms 定时器，二选一，保持简单）批量 flush：一次 `set()` 把所有待拼接内容合并进消息。
  - 结构性事件（`tool_call` / `tool_result` / `subagent_*` / `finished` / `error` 等）到达时**先强制 flush 再处理**，保证气泡边界语义不变。
- 验收：流式期间 React 重渲染次数从"每 token 一次"降为"每帧一次"（约 20-60 次/秒封顶），UI 无可见延迟增加。

### 3.2 平滑打字机（对齐 `useSmoothStreamingContent`）

**方案**：新建 `frontend/src/components/chat/useSmoothContent.ts`，移植 DeerFlow `markdown-content.tsx:52` 的思路（自研，不引依赖）：

- 输入：目标文本 `target`、是否流式中 `isStreaming`。
- 行为：当 `target` 以当前显示文本为前缀且 delta ≥ 阈值（DeerFlow 用 80 字符）时，用 rAF 在 ~300ms 内按每帧 ≥8 字符渐进揭示；非前缀关系（新消息/重置）直接跳变。
- 在 `MessageItem.tsx` 的 AI 正文渲染处消费该 hook 的输出作为 Markdown 输入。
- 注意：与 3.1 的节流叠加后，数据层节流 + 展示层平滑，效果与 DeerFlow 一致。

### 3.3 流式渲染分级

- `MessageItem.tsx`：流式中的那条 AI 消息（`isStreaming && 是最后一条生长中的消息`）代码块降级为纯 `<pre>` 不做语法高亮，流式结束后恢复高亮。实现方式：给 `MessageItem` 传 `isLiveStreaming` prop，代码块渲染分支据此切换。
- 可选：将 ReactMarkdown 替换为 Streamdown（DeerFlow 同款，`parseIncompleteMarkdown` 容忍未闭合标签）。**建议先不做**，除非流式中出现明显的 markdown 截断渲染问题——这是独立决策，不阻塞本阶段。

### 3.4 Reasoning/Thinking 自动收起（对齐核心行为）

**问题**：`MessageItem.tsx:182-210` 的 ThinkingCard 默认展开且永不自动收起，历史加载也展开，与 ProcessGroup 默认收起的行为不一致。

**方案**（对齐 DeerFlow `reasoning.tsx`）：

- ThinkingCard 增加 `isStreaming` 感知：流式中展开、标题显示动画态（如 "思考中..." + 计时）；流式结束（`finished`/正文开始/组件收到完成信号）**1 秒后自动收起一次**。
- 用 `hasAutoClosedRef` 保证只自动收一次：用户手动展开后不再干预。
- 完成后标题显示 "思考了 N 秒"（需要在 thinking 消息上记录起止时间：`chat-store.ts` 收到首个 `thinking` 事件记 `thinkingStartAt`，气泡边界/正文开始时记 `thinkingEndAt`）。
- 历史加载的 thinking 消息默认**收起**（与 ProcessGroup 一致），消除现状的不一致。
- 联动：thinking 消息通常被包进 ProcessGroup 内（组默认收起），此时单卡状态不冲突——组展开后看到的 ThinkingCard 应为收起态。

---

## 4. Phase 2：过程组折叠交互对齐（纯前端）

### 4.1 ProcessGroup 改为"只展示最新"

**现状**：`MessageList.tsx:19-46` 分组 + `ProcessGroup.tsx` 整组折叠，流式中最新组整体展开，结束后整组收起。

**DeerFlow 模式**（`message-group.tsx`）：

- 组内步骤（tool_call / tool_result / thinking / 正文片段）中：
  - **最后一个工具调用常驻展示**（不折叠），让用户始终看到"当前在做什么"；
  - 最后一个工具调用**之前**的步骤折叠为 "还有 N 步 / 收起" 按钮（默认收起）；
  - 最后一段 thinking 单独一个折叠按钮（默认收起）。
- 结束后：整组仍可整体折叠为 "执行过程 · N 步"（保留本项目现有的组级收起，二者结合）。

**方案**：改造 `ProcessGroup.tsx`：

- 展开态内部从"平铺所有步骤"改为两段式：历史步骤区（默认折叠，`还有 N 步` 按钮）+ 最新步骤区（常驻）。
- 保留现有的 `userToggledRef` 尊重用户手动选择逻辑、480px 限高滚动、自动滚底。
- 步骤摘要 `summarize()` 逻辑复用。

### 4.2 折叠组件统一

- 引入一个轻量 `Collapsible` 封装（可以不引 Radix，用现有 tailwind 写 `grid-rows-[0fr]↔[1fr]` 或 max-height 过渡），统一 ThinkingCard / ToolCallCard / ProcessGroup 的展开动画与 Chevron 旋转。
- 优先级低，可在 4.1 改造时顺手做，不单独排期。

### 4.3 明确不改的部分

- SubAgent 右侧面板模式保留（本项目特色，DeerFlow 的就地 SubtaskCard 并非明显更优）。
- 长文本控制手段（后端截断 + 限高滚动）保留。

---

## 5. Phase 3：断线续传（前后端配合）

**现状**：SSE 无事件 id；刷新后 5s 轮询 `GET /api/execute/{id}/status`，结束后整体拉历史，期间看不到增量输出。

**方案**（对齐 DeerFlow 的 `Last-Event-ID` 思路，但按本项目协议裁剪，不引入完整 Platform API）：

1. 后端 `app/api/execute.py`：为每条 SSE 事件分配单调递增序号，输出格式加 `id: <seq>` 行（`sse-client.ts` 解析时读取）。
2. App 层增加事件环形缓冲（内存，按 thread_id 存最近 N 条事件，或复用落库的中间事件表）—— DeerFlow 用事件日志 + `gap` 事件，本项目用内存缓冲即可，缓冲丢失时回退现有轮询。
3. 前端 `global-sse.ts` / `sse-client.ts`：
   - 记录每个 thread 最后收到的事件 id（内存 + sessionStorage，对齐 DeerFlow 的 `lg:stream:{threadId}` 思路）；
   - 页面刷新后若该 thread 仍在运行，用 `Last-Event-ID`（或自定义 header/query 参数）重连 `POST /api/execute/{thread_id}/resume`，后端从缓冲补发缺失事件后续流；
   - 补发失败（缓冲已丢）则回退现有轮询逻辑。
4. 验收：刷新页面后流式输出从断点继续，而非等待结束后一次性呈现。

**风险点**：重连不能重发 POST body 重新执行（现状 `sse-client.ts` 的 reconnect 有此隐患，`maxReconnectAttempts: 0` 实际上是规避它）——续传必须走独立的 resume 端点，绝不复用 execute 端点。

---

## 6. Phase 4（可选，暂不实施）：协议级迁移到 langgraph-sdk

仅记录结论，供将来立项参考：

- DeerFlow 前端流式层 = `@langchain/langgraph-sdk` 的 `useStream` + 后端自实现的 LangGraph Platform API（`POST /api/threads/{tid}/runs/stream`、`values`/`messages-tuple`/`custom` 事件、`[chunk, metadata]` 二元组、按 `message.id` concat）。
- 本项目要对等到这一层，后端需实现：threads/runs 资源模型、事件持久化与序号、`messages-tuple` 序列化（LangChain message chunk 格式）、状态快照（values）。相当于重写 harness 的 API 层。
- 收益（SDK 接管 delta 合并、重连、throttle）在 Phase 1+3 完成后大部分已被覆盖，**建议默认不做**。

---

## 7. 实施顺序与工作量估计

| 阶段 | 内容 | 改动范围 | 估计工作量 |
|---|---|---|---|
| 1 | 渲染节流、平滑打字机、渲染分级、Thinking 自动收起 | `chat-store.ts`、`MessageItem.tsx`、新增 `useSmoothContent.ts` | 1-2 天 |
| 2 | ProcessGroup "只展示最新"、折叠组件统一 | `ProcessGroup.tsx`、`MessageList.tsx`、新增 `Collapsible` | 1 天 |
| 3 | 事件序号 + 断线续传 | `app/api/execute.py`、`sse-client.ts`、`global-sse.ts`、`chat-store.ts` | 2-3 天 |
| 4 | Platform 协议迁移（可选） | harness + app + frontend 大范围 | ≥2 周 |

每个阶段独立可交付、可回滚；Phase 1 完成即可获得 DeerFlow 的大部分体感提升。

---

## 附录：关键文件索引

### 本项目
- `frontend/src/lib/sse-client.ts:20-141` — SSE 客户端
- `frontend/src/lib/global-sse.ts:19-99` — 全局连接管理
- `frontend/src/lib/chat-store.ts:83-695` — zustand store + `handleSSEEvent`（`_appendToMessage` 在 :144-209）
- `frontend/src/lib/types.ts:59-122` — 事件协议类型
- `frontend/src/components/chat/ChatPanel.tsx:131-347` — 发送/停止/订阅/轮询
- `frontend/src/components/chat/MessageList.tsx:19-46` — 过程消息分组
- `frontend/src/components/chat/ProcessGroup.tsx:38-88` — 执行过程折叠组
- `frontend/src/components/chat/MessageItem.tsx:182-210` — ThinkingCard
- `frontend/src/components/chat/ToolCallCard.tsx:13-60` — 工具调用折叠卡
- `app/api/execute.py:43-161` — App 层 SSE 转发 + 落库
- `harness/api/routers.py:44-56`、`harness/main.py:1389-1517` — 事件源头

### DeerFlow
- `frontend/src/core/threads/hooks.ts` — `useThreadStream`（:1408）、`mergeMessages`（:462）、`stopThread`（:1754）
- `frontend/src/core/api/api-client.ts:236-373` — SDK 包装（replay gap、重连、取消）
- `frontend/src/core/messages/utils.ts:36` — `getMessageGroups`；:482 `splitInlineReasoning`；:573 reasoning 提取
- `frontend/src/components/workspace/messages/markdown-content.tsx:52` — `useSmoothStreamingContent`
- `frontend/src/components/workspace/messages/message-group.tsx:317-421` — "N more steps" 折叠
- `frontend/src/components/ai-elements/reasoning.tsx:45,93-103` — 自动收起（1s 延迟、`hasAutoClosed`）
- `backend/app/gateway/routers/thread_runs.py:845` — `runs/stream` 端点；`services.py:119` — `format_sse`
- `backend/packages/harness/deerflow/runtime/runs/worker.py` — 事件生产

---

# 第二部分：文件上传 / 文件输出 / 内容展示 对比与对齐方案

## 8. 文件上传对比

| 维度 | 本项目 | DeerFlow |
|---|---|---|
| 上传时机 | 选中即逐文件上传（`ChatPanel.tsx:74-125`） | 提交消息时统一上传（`hooks.ts:1932 sendMessage`） |
| 入口 | 仅回形针文件对话框 | 对话框 + **粘贴** + **拖拽**（`prompt-input.tsx:659,921-948`） |
| 前端校验 | 无（等后端报错） | 从 `GET .../uploads/limits` 拉限制做本地预检 + toast（`input-box.tsx:521-552`） |
| 上传中 UI | chip 上 spinner | **乐观消息三段式**：附件卡 status:uploading → `element:"task"` 占位 AI 消息（Task 折叠卡带 Loader）→ 上传完成改写乐观消息 |
| 消息字段 | `files: [{filename,size,path,mime_type}]`（`types.ts:148-157`） | `additional_kwargs.files: [{filename,size,path(虚拟路径),status}]`，**不含二进制** |
| 注入 agent | `UploadsMiddleware` 前置 `<uploaded_files>` 块到 HumanMessage（`harness/middleware/uploads.py:248-268`） | 相同思路：`<current_uploads>` 块 + **文档大纲（标题+行号）**，并指示 agent 用 `read_file`/`grep`/`glob` 按需读取 |
| 文档转换 | **断链**：prompt 和 middleware 都引用"配套 .md"，但全仓无转换实现 | 可选 `uploads.auto_convert_documents`（pymupdf4llm / markitdown）生成 `.md` 副本，默认关 |
| 图片 | 仅路径注入 prompt，**模型看不到图** | 同样不内联 vision，但提供 `view_image` 工具让 agent 主动看图 |
| 历史文件 | middleware 扫描 uploads 目录每轮注入 | 不再每轮注入，agent 用 `list_uploaded_files` 工具按需发现 |
| 历史消息展示 | 仅 `[Attached N file(s): ...]` 纯文本 | 文件卡片 `RichFilesList`（图片缩略图）；还能从 content 里的标签块正则解析回卡片（`parseUploadedFiles`，`utils.ts:822-865`） |
| 安全 | 文件名消毒、`O_NOFOLLOW`、MIME+扩展名白名单 + 8KB 嗅探 | 路径穿越校验、临时文件 staging + `os.replace` 原子提交、用户内容 `neutralize_untrusted_tags` 防注入 |

**结论**：上传的基础链路（multipart 上传 → 虚拟路径 → prompt 注入）两边结构几乎一致，DeerFlow 强在：① 粘贴/拖拽入口；② 前端预校验；③ 乐观 UI；④ 文档转换管线真实存在；⑤ `view_image` / `list_uploaded_files` 工具补齐了"模型感知文件"的能力；⑥ 历史消息有文件卡片。

## 9. 文件输出（产物）对比 —— 差距最大的一块

| 维度 | 本项目 | DeerFlow |
|---|---|---|
| 产出目录 | `/mnt/user-data/outputs` 有约定有落盘，prompt 反复强调 | 相同约定 |
| 产物登记 | **state 里 `artifacts` 字段是死字段**（`harness/models.py:304`，无人写入） | agent 调 **`present_files` 工具** → `Command(update={"artifacts": ...})`，reducer 合并去重（`present_file_tool.py:83-121`） |
| 列表/下载 API | **无**（`app/api/files.py` 只服务 uploads；`filesAPI.download` 前端零调用） | `GET /api/threads/{tid}/artifacts/{path}`：HTML/SVG 强制下载防 XSS、文本内联、二进制支持 Range；`.skill` 包内成员读取 |
| 消息内展示 | **无**，只能靠 agent 文本里提路径 | `present_files` tool_call → `assistant:present-files` 组 → `ArtifactFileList` 文件卡片（图标+文件名+下载按钮） |
| 预览面板 | **无**（SubagentDetailPanel 只看 subagent 过程，不是通用面板） | 右侧可拖拽 Artifact 面板：code/preview 切换、markdown/html 预览、PDF/图片/音视频 iframe、复制/新窗口/下载 |
| 流式写文件 | 无 | `write_file`/`str_replace` 的 tool_call 流式期间从 `args.content` 构建草稿，自动打开面板 3s 节流预览（不经过后端） |
| 正文内引用 | 沙箱路径图片/链接前端无法解析 | markdown 里的 `/mnt/...` 链接和图片解析为 artifact URL（`core/artifacts/utils.ts:89-124`） |

**结论**：本项目"agent 产出 → 用户可见"的链路是断的——文件落在宿主机 outputs 目录后没有出口。DeerFlow 的完整闭环是：**`present_files` 工具（显式登记）→ artifacts state → 消息内卡片 + 右侧面板 + artifact 下载/预览端点**。

## 10. 内容展示对比

| 能力 | 本项目 | DeerFlow |
|---|---|---|
| Markdown | react-markdown + remark-gfm（表格✓） | Streamdown + remark-gfm，`parseIncompleteMarkdown` 容忍未闭合 |
| 数学公式 | ❌（无 remark-math/katex） | ✅ remark-math + rehype-katex |
| Mermaid | ❌（**但 prompt 鼓励模型输出 ```mermaid**，能力不匹配） | ✅ `@streamdown/mermaid` |
| 代码块 | react-syntax-highlighter 20 语言 + 语言标签栏，**无复制按钮** | `@streamdown/code`，流式期间轻量渲染结束后全量高亮 |
| Citation | prompt 约定 `[citation:...](url)`，前端渲染为普通链接 | `CitationLink` 徽章 + HoverCard 预览 + 消息底部来源面板（`core/citations/`） |
| 链接安全 | 普通 `<a target="_blank">` | 协议白名单（`javascript:` 渲染为禁用 span） |
| human 消息 | 按 Markdown 渲染 | **纯文本不渲染**（防粘贴代码/日志被解析坏，`message-list-item.tsx:485-489`） |
| 右侧面板 | 仅 SubagentDetailPanel（420px，subagent 过程） | 三种互斥面板：artifacts / browser（实时浏览器画面）/ sidecar |
| 特殊卡片 | ThinkingCard / ToolCallCard / SubAgentCard / ClarificationDialog / Todo chips | Reasoning / ChainOfThought / SubtaskCard / HumanInputCard（结构化表单协议 `human_input_request/response`）/ TodoList / 乐观上传 Task 卡 |

## 11. 对齐方案（文件与内容展示部分，作为 Phase 5-7 追加到原路线图）

### Phase 5：产物出口闭环（最高优先，前后端配合）

目标：把已落盘的 outputs 暴露给用户，对齐 DeerFlow 的最小闭环。

1. **harness 侧**：新增 `present_files` 工具（参照 `deerflow/tools/builtins/present_file_tool.py`）：
   - 入参 `filepaths: list[str]`，只允许 `/mnt/user-data/outputs/` 内路径，规范化后写入 state `artifacts`（激活现有死字段，reducer 合并去重）；
   - 同时推送新 SSE 事件 `type:"present_files"`（或复用 tool_call 事件由前端识别工具名，二选一——**建议复用 tool_call**，前端按 `tool_name === "present_files"` 归组，改动最小，协议不膨胀）。
2. **app 侧**：`app/api/files.py` 新增 `GET /files/outputs/{thread_id}/{path}` 下载/预览端点：
   - 对齐 DeerFlow 安全规则：HTML/SVG 强制 attachment 下载、文本内联、二进制 inline + Range 支持、`?download=true` 强制下载、路径穿越防护。
3. **前端**：
   - `MessageList` 分组新增 `present-files` 类型（识别 `present_files` tool_call），渲染 `ArtifactFileList` 式文件卡片（图标 + 文件名 + 下载按钮）；
   - markdown 渲染处把 `/mnt/user-data/...` 的链接和图片 src 映射为下载端点 URL（对齐 `resolveMarkdownArtifactURL`）。
4. 验收：agent 生成文件 → 消息内出现文件卡片 → 点击可下载/预览文本类文件。

### Phase 6：右侧 Artifact 预览面板（纯前端为主）

- 复用现有 SubagentDetailPanel 的右侧栏模式，改为可切换的通用面板：subagent 详情 / artifact 预览。
- 面板能力分级实施：第一版只做文本/代码（只读高亮）+ markdown 预览 + 下载；html 预览走 sandboxed iframe 第二版再做。
- 流式写文件草稿预览（从 `write_file` tool_call 的 `args.content` 构建）可作为后续增强，不阻塞。

### Phase 7：渲染能力补齐与上传体验（纯前端，可拆小迭代）

按 ROI 排序的独立小项：

1. **代码块复制按钮**（半小时，体验提升明显）。
2. **Mermaid 渲染**——prompt 已承诺，必须补（`mermaid` npm 包 + 代码块分支渲染）。
3. **数学公式**（remark-math + rehype-katex）。
4. **Citation 组件**：`[citation:Title](url)` 渲染为徽章 + 消息底部来源列表。
5. **human 消息改为纯文本渲染**（防粘贴内容被 Markdown 解析坏）。
6. **上传体验**：粘贴/拖拽上传、前端预校验（大小/类型，后端已有白名单可暴露 limits）、历史消息附件文件卡片。
7. **harness 侧工具补齐**：`view_image`、`list_uploaded_files`；修复或移除 prompt 中"配套 .md"的断链承诺（要么实现转换管线，要么删掉该描述）。
8. 链接协议白名单。

### 更新后的实施顺序

| 阶段 | 内容 | 估计工作量 |
|---|---|---|
| 1 | 流式渲染性能 + 打字机 + Thinking 自动收起 | 1-2 天 |
| 2 | ProcessGroup "只展示最新" | 1 天 |
| 3 | SSE 断线续传 | 2-3 天 |
| **5** | **产物出口闭环（present_files + 下载端点 + 文件卡片）** | **2-3 天** |
| **6** | **右侧 Artifact 预览面板** | **2 天** |
| **7** | **渲染能力补齐（复制按钮/Mermaid/KaTeX/citation）+ 上传体验** | **拆小迭代，共 2-3 天** |
| 4 | Platform 协议迁移（可选，默认不做） | ≥2 周 |

**优先级建议**：Phase 5 的实际用户价值高于 Phase 2/3——产物出口是"完全缺失"而非"体验不够好"，建议在 Phase 1 完成后优先做 Phase 5。

### 附录 B：文件与展示相关关键文件索引

本项目：
- `frontend/src/components/chat/InputBar.tsx:133-180` — 附件 UI
- `frontend/src/components/chat/ChatPanel.tsx:74-125` — 上传逻辑
- `app/api/files.py:23-51`、`app/services/file_service.py` — 上传端点与落盘
- `harness/middleware/uploads.py:248-268` — `<uploaded_files>` prompt 注入
- `harness/models.py:304` — `artifacts` 死字段（Phase 5 挂点）
- `harness/config/paths.py:254` — outputs 目录约定
- `frontend/src/components/chat/MessageItem.tsx:64-179` — Markdown renderer 全集

DeerFlow：
- `frontend/src/core/uploads/*`、`frontend/src/components/ai-elements/prompt-input.tsx:659,921-948` — 上传（粘贴/拖拽/校验）
- `backend/app/gateway/routers/uploads.py:299` — 上传端点
- `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py:202-293` — 上下文注入
- `backend/packages/harness/deerflow/tools/builtins/present_file_tool.py:83-121` — present_files 工具
- `backend/app/gateway/routers/artifacts.py:153` — artifact 下载/预览端点
- `frontend/src/components/workspace/artifacts/artifact-file-list.tsx`、`artifact-file-detail.tsx` — 文件卡片与预览面板
- `frontend/src/core/artifacts/preview.ts:81`、`loader.ts:27-56` — 流式写文件草稿
- `frontend/src/core/streamdown/plugins.ts` — markdown 插件集（katex/mermaid/code）
- `frontend/src/core/citations/*` — citation 徽章与来源面板
