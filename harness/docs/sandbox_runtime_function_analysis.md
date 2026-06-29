┌─ Langfuse Trace (thread_id, user_id, run_name) ────────────────────────────┐
│                                                                             │
│  ┌─ abefore_agent (正向 0→19) ────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  [0] ThreadDataMiddleware                                           │    │
│  │      └─ 创建 ~/.multiagent-studio/users/{uid}/threads/{tid}/        │    │
│  │         workspace/ + uploads/ + outputs/                            │    │
│  │      └─ 写入 state["thread_data"]、state["workspace"]               │    │
│  │                                                                     │    │
│  │  [1] UploadsMiddleware                                              │    │
│  │      └─ 扫描上传目录，将文件列表注入 HumanMessage                    │    │
│  │                                                                     │    │
│  │  [2] SandboxMiddleware                                              │    │
│  │      └─ 初始化沙箱 provider，存储 sandbox_id 到 state               │    │
│  │                                                                     │    │
│  │  [9] DynamicContextMiddleware                                       │    │
│  │      └─ 首次对话: 注入 <system-reminder>                            │    │
│  │         <memory>                                                    │    │
│  │           User Context: ...                                          │    │
│  │           Facts: [preference|0.9] 用户偏好简洁回答...                 │    │
│  │         </memory>                                                    │    │
│  │         <current_date>2026-06-28, Sunday</current_date>             │    │
│  │         </system-reminder>                                           │    │
│  │      └─ 同天: 跳过                                                  │    │
│  │                                                                     │    │
│  │  [17] LoopDetectionMiddleware.abefore_agent                         │    │
│  │      └─ 清理同一 thread 中其他 run 的陈旧 pending warnings           │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─ abefore_model (正向 0→19) ────────────────────────────────────────┐    │
│  │  [9] SummarizationMiddleware (条件性 — 当前未激活)                   │    │
│  │      └─ 如果激活: 检查 token 阈值 → 压缩旧消息 → 触发 memory_flush  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─ awrap_model_call (嵌套洋葱: 后注册=外层) ─────────────────────────┐    │
│  │                                                                     │    │
│  │  [17] LoopDetectionMiddleware ← 最外层                             │    │
│  │      └─ drain pending warnings → 注入 hidden HumanMessage           │    │
│  │      └─ 警告示例: "[LOOP DETECTED] You are repeating..."            │    │
│  │        │                                                            │    │
│  │  [4] LLMErrorHandlingMiddleware ← 中层                               │    │
│  │      └─ try: handler(request)                                       │    │
│  │      └─ 429/5xx → 指数退避重试 (最多3次)                            │    │
│  │      └─ 全部失败 → 返回合成 AIMessage (不崩溃)                       │    │
│  │        │                                                            │    │
│  │  [3] DanglingToolCallMiddleware ← 最内层 (最靠近 LLM)               │    │
│  │      └─ 修补: 检查 history 中是否有 AIMessage(tool_calls) 但缺      │    │
│  │         ToolMessage → 注入占位 ToolMessage                          │    │
│  │        │                                                            │    │
│  │        实际 LLM 调用: model.invoke(messages)                        │    │
│  │        ├─ Langfuse: generation span (input_tokens, output_tokens)    │    │
│  │        └─ 返回 AIMessage(                                           │    │
│  │             content="好的，让我来读一下文件",                        │    │
│  │             tool_calls=[{name: "file_read", args: {path: "..."}}]    │    │
│  │           )                                                          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─ aafter_model (反向 19→0) ─────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  [18] SafetyFinishReasonMiddleware ← 第1个运行 (逆序链最前)         │    │
│  │      └─ 检查 finish_reason: "content_filter"? "refusal"?            │    │
│  │      └─ 正常 → pass                                                 │    │
│  │                                                                     │    │
│  │  [17] LoopDetectionMiddleware.aafter_model ← 第2个运行              │    │
│  │      └─ 提取 tool_calls → _hash_tool_calls() → MD5                  │    │
│  │      └─ 与 thread 历史比较: 首次出现 → 记录到 sliding window        │    │
│  │      └─ 重复 ≥ warn_threshold(3次) → 排队警告                       │    │
│  │      └─ 重复 ≥ hard_limit(5次) → 剥离 tool_calls, 强制文本回答      │    │
│  │                                                                     │    │
│  │  [16] SubagentLimitMiddleware (未激活)                               │    │
│  │                                                                     │    │
│  │  [13] TitleMiddleware                                               │    │
│  │      └─ 首次交换? → 调用轻量 LLM 生成标题 → state["suggested_title"]│    │
│  │      └─ Langfuse: generation span tagged "middleware:title"          │    │
│  │                                                                     │    │
│  │  [12] TokenUsageMiddleware                                           │    │
│  │      └─ 提取 AIMessage.usage_metadata                               │    │
│  │      └─ 累加到 state["token_usage"]: {prompt_tokens, completion}     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─ 工具节点 (Tool Node) ─────────────────────────────────────────────┐    │
│  │  对每个 tool_call 依次经过 awrap_tool_call 洋葱:                     │    │
│  │                                                                     │    │
│  │  [19] ClarificationMiddleware (最外层)                              │    │
│  │      └─ tool_name == "ask_clarification"?                           │    │
│  │      └─ 是 → 构造 ToolMessage + ClarificationRequest                │    │
│  │      └─ 返回 Command(goto=END) → 中断执行!                          │    │
│  │      └─ 否 → pass                                                   │    │
│  │        │                                                            │    │
│  │  [8] ToolErrorHandlingMiddleware                                     │    │
│  │      └─ try: handler(request)                                       │    │
│  │      └─ 工具异常 → 重试 (最多3次) → 失败返回 error ToolMessage       │    │
│  │        │                                                            │    │
│  │  [7] SandboxAuditMiddleware                                          │    │
│  │      └─ 审计 file_read: 低风险 → debug log                          │    │
│  │      └─ 审计 bash: 检查 HIGH/MEDIUM 模式 → 日志                     │    │
│  │        │                                                            │    │
│  │  [6] GuardrailMiddleware (未激活)                                    │    │
│  │        │                                                            │    │
│  │  [2] SandboxMiddleware (最内层)                                      │    │
│  │      └─ 注入沙箱上下文 (thread_id, workspace, user_id)              │    │
│  │        │                                                            │    │
│  │        实际工具执行: file_read(config.yaml)                          │    │
│  │        └─ Langfuse: span (tool_name, input, output)                  │    │
│  │        └─ 返回 ToolMessage(content="models:\n  - name: gpt-4o...")  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│          下一轮 ReAct 循环 ← 模型看到 ToolMessage                          │
│                                    │                                        │
│  ┌─ abefore_model → LLM 调用 → aafter_model ──────────────────────────┐    │
│  │  LLM 看到:                                                          │    │
│  │    ToolMessage(file_read 返回 config.yaml 内容)                      │    │
│  │    → AIMessage(content="config.yaml 内容如下: models 节定义了...")   │    │
│  │    → 无 tool_calls → ReAct 循环结束                                  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─ aafter_agent (反向 19→0) ─────────────────────────────────────────┐    │
│  │  [14] MemoryMiddleware                                               │    │
│  │      └─ filter_messages_for_memory(): 只保留 Human + 最终AI回复      │    │
│  │      └─ detect_correction() → "不对"/"你理解错了" 信号               │    │
│  │      └─ detect_reinforcement() → "完全正确" 信号                     │    │
│  │      └─ queue.add(thread_id, messages, agent_name, user_id)          │    │
│  │      └─ → 30s debounce → LLM 分析 → 更新 memory.json                │    │
│  │                                                                     │    │
│  │  [17] LoopDetectionMiddleware.aafter_agent                          │    │
│  │      └─ 清理当前 thread/run 的 pending warnings                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  StreamBridge → SSE events → 前端渲染                                        │
│  RunJournal → flush → EventStore (SQLite/内存)                              │
│  Langfuse.finalize_trace(trace_id, "success")                               │
└─────────────────────────────────────────────────────────────────────────────┘