/**
 * 全局 TypeScript 类型定义
 * 与 Harness models.py 和 App 服务 schemas 保持一致
 */

// ===== Harness 相关 (对齐 harness/models.py) =====
export interface SubAgentConfig {
  name: string;
  display_name: string;
  description: string;
  system_prompt: string;
  model: string;
  tools: string[] | null;
  disallowed_tools: string[];
  temperature: number;
  max_turns: number;
  /** Wall-clock timeout in seconds (default: 900 = 15 min). Added in v2 refactor. */
  timeout_seconds: number;
}

/** SubAgent execution result — aligned with harness/models.py SubAgentResult. */
export interface SubAgentResultData {
  task_id: string;
  trace_id: string;
  status: "success" | "error" | "max_iterations_reached" | "cancelled" | "timed_out";
  output: string;
  error?: string | null;
  iterations: number;
  ai_messages: Record<string, unknown>[];
  token_usage_records: TokenUsageRecord[];
  started_at?: string | null;
  completed_at?: string | null;
}

/** Per-LLM-call token usage record from SubagentTokenCollector. */
export interface TokenUsageRecord {
  source_run_id: string;
  caller: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface AgentNode {
  id: string;
  type: "lead" | "subagent";
  config: SubAgentConfig;
  position: { x: number; y: number };
  connections: string[];
}

export interface ExecutionGraph {
  nodes: AgentNode[];
  edges: [string, string][];
  entry_point: string;
}

// ===== SSE 事件 (对齐 SSEEventType) =====
export type SSEEventType =
  | "message"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "subagent_start"
  | "subagent_progress"
  | "subagent_end"
  | "subagent_thinking"
  | "subagent_tool_call"
  | "subagent_tool_result"
  | "clarification"
  | "todo_update"
  | "title_update"
  | "memory_update"
  | "token_usage"
  | "evaluation"
  | "error"
  | "finished"
  | "context_cleared"
  // ── Agent Team 事件 ──
  | "team_start" | "team_end" | "team_status"
  | "team_task_update" | "team_message" | "member_status"
  | "team_error" | "team_degrade"
  | "message_injected"
  // ── 断线续传 (Phase 3): resume 不可续传时的一次性通知, 前端回退轮询 ──
  | "resync"
  // ── 前端合成事件: 连接异常终止 (非 2xx / 断流且无终态), 由 global-sse 注入 ──
  | "connection_lost";

export interface SSEEvent {
  type: SSEEventType;
  content?: string;
  thread_id?: string;
  msg_type?: string;
  tool_name?: string;
  tool_args?: Record<string, unknown>;
  tool_result?: string;
  subagent_name?: string;
  agent_name?: string;
  instruction?: string;
  request?: ClarificationRequest;
  todo?: TodoItem;
  /** plan 模式整表替换 (todo_update 事件, 优先于单条 todo) */
  todos?: TodoItem[];
  title?: string;
  tokens?: TokenUsage;
  trace_id?: string;
  status?: string;
  duration_ms?: number;
  /** SubAgentResult fields (populated on subagent_end events). */
  subagent_result?: SubAgentResultData;
  /** Progress fields (populated on subagent_progress events). */
  iterations?: number;
  max_turns?: number;
  current_step?: string;
  current_task_id?: string;
  // ── Agent Team 字段 ──
  project_id?: string;
  phase?: string;
  task?: ProjectTask;
  task_id?: string;
  task_title?: string;
  started_at?: string;
  message?: TeamMessage;
  members?: string[];
  mode?: string;
  total_rounds?: number;
  reason?: string;
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  model?: string;
}

export interface TodoItem {
  id: string;
  description: string;
  status: "pending" | "in_progress" | "completed" | "failed";
  assigned_agent: string | null;
}

export interface ClarificationRequest {
  id: string;
  question: string;
  context: string;
  options: string[] | null;
  required: boolean;
}

// ===== 上传文件附件 =====
export interface AttachedFile {
  id: string; // 本地生成的唯一 id — 同名附件按 id 区分
  filename: string;
  original_name?: string;
  mime_type?: string;
  size_bytes: number;
  virtual_path: string;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

// ===== 消息 =====
export interface ChatMessage {
  id: string;
  role: "human" | "ai" | "tool" | "subagent" | "system";
  content: string;
  msgType: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  tokenCount: number;
  /** thinking 气泡首个 thinking chunk 的时间 (Date.now(), ms) — 仅流式期间记录, 不落库 */
  thinkingStartAt?: number;
  /** thinking 气泡结束时间 (正文开始 / tool_call / finished 等) — 用于 "思考了 N 秒" 与自动收起 */
  thinkingEndAt?: number;
}

// ===== 会话 =====
export interface ThreadSummary {
  id: string;
  title: string;
  status: "idle" | "running" | "suspended" | "finished" | "error";
  presetType: string | null;
  createdAt: string;
  updatedAt: string;
  // ── Agent Team 字段 ──
  project_id?: string;
  agent_name?: string;
  mode?: string;
}

export interface ThreadDetail {
  id: string;
  userId: string;
  title: string;
  status: string;
  presetType: string | null;
  executionGraph: ExecutionGraph | null;
  isArchived: boolean;
  createdAt: string;
  updatedAt: string;
  // ── Agent Team 字段 ──
  project_id?: string;
  agent_name?: string;
  mode?: string;
}

// ===== 用户 =====
// 与后端 app/schemas/auth.py UserResponse 对齐 (snake_case)
export interface User {
  id: string;
  email: string;
  username: string;
  display_name: string;
  role: "user" | "admin";
  avatar_url: string;
  is_active: boolean;
}

export interface AuthState {
  user: User | null;
  accessToken: string | null;
  isAuthenticated: boolean;
}

// ===== 预设 =====
export interface PresetAgent {
  name: string;
  display_name: string;
  description: string;
}

// ===== 工具组 =====
export interface ToolGroup {
  name: string;
  description: string;
  tools: string[];
}

// ===== Agent Team 相关 (对齐后端 harness/team/models.py) =====

export interface Project {
  id: string;
  user_id?: string;
  name: string;
  description: string;
  members: string[];
  lead_agent?: string;
  thread_count: number;
  task_count: number;
  created_at: string;
  updated_at: string;
}

/** 任务状态 — 与后端 TeamTaskStatus 对齐 (含 Review 流程).
 *  pending → in_progress → in_review → approved (终态)
 *                        ↘ completed (终态, 兼容)
 *                        ↘ revision_needed → in_progress
 *                        ↘ failed / cancelled (终态)
 */
export type ProjectTaskStatus =
  | "pending" | "in_progress"
  | "in_review" | "approved" | "revision_needed"
  | "completed" | "failed" | "cancelled";

/** 结构化任务规格 — 对齐后端 TaskSpec (Phase 2 任务协议 JSON 化). */
export interface TaskSpec {
  background?: string;
  goal?: string;
  description?: string;
  constraints?: string[];
  format?: string;
  acceptance_criteria?: string[];
}

/** 成员完成输出的结构化结果 — 对齐后端 TaskResult. */
export interface TaskResult {
  status?: string;
  output?: string;
  evidence?: string[];
  uncertainty?: "low" | "medium" | "high";
  failure_reason?: string;
}

export interface ProjectTask {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: ProjectTaskStatus;
  assigned_agent?: string;
  dependencies: string[];
  priority: "low" | "medium" | "high" | "critical";
  output?: string;               // 成员执行结果文本
  spec?: TaskSpec | null;        // 结构化任务规格 (可选)
  result?: TaskResult | null;    // 结构化完成结果 (可选)
  review_feedback?: string;      // Lead/Verifier 审查反馈 (REVISION_NEEDED 时)
  revision_count?: number;       // 修改轮次
  risk?: "low" | "high" | null;  // 风险分级 (Phase 3; null=未分级/历史任务)
  error?: string;                // 失败原因
  retry_count?: number;          // crash 恢复重试次数
  result_summary?: string;
  result_detail?: Record<string, any>;
  created_at: string;
  updated_at: string;
}

/** 成员运行时状态 — 对齐后端 TeammateStatus 枚举.
 *  spawning → working ↔ idle → shutting_down → shutdown / failed
 *  ("busy" 为后端枚举外取值, 语义等同 working, 需兼容)
 */
export type TeamMemberRuntimeStatus =
  | "spawning" | "idle" | "working" | "busy"
  | "shutting_down" | "shutdown" | "failed";

export interface TeamMemberRuntime {
  agent_name: string;
  display_name?: string;
  status: TeamMemberRuntimeStatus;
  current_task_id?: string;
  current_task_title?: string;
  last_result?: string;
  /** ISO 时间戳 — status=working 时记录开始时间，前端用于显示计时 */
  started_at?: string;
}

/** 团队消息类型 — 对齐后端 TeamMessageType 枚举. */
export type TeamMessageType =
  | "text" | "broadcast" | "lifecycle"
  | "shutdown_request" | "shutdown_response"
  | "plan_approval_request" | "plan_approval_response";

export interface TeamMessage {
  id: string;
  from_agent: string;
  to_agent?: string;
  msg_type: TeamMessageType;
  content: string;
  task_id?: string;
  timestamp: string;
}

export interface TeamExecutionState {
  isRunning: boolean;
  members: TeamMemberRuntime[];
  tasks: ProjectTask[];
  messages: TeamMessage[];
  currentRound: number;
  maxRounds: number;
}

/** Agent 能力卡片 (对齐后端 AgentCard) */
export interface AgentCard {
  name: string;
  display_name: string;
  description: string;
  tools: string[];
  skills: string[];
  model: string;
  role: "lead" | "member";
  created_at: string;
  updated_at: string;
}

/** Agent 记忆配置 (对齐后端 AgentMemoryFields) */
export interface AgentMemoryConfig {
  backend: "file" | "mem0";
  max_facts: number;
  injection_enabled: boolean;
  max_injection_tokens: number;
  mem0_tool_enabled: boolean;
  mem0_search_top_k: number;
}

/** Agent 功能开关 (对齐后端 AgentFeaturesFields) */
export interface AgentFeaturesConfig {
  summarization: boolean;
  subagent: boolean;
  langfuse: boolean;
  guardrail: boolean;
}

/** 自定义 Agent 定义 (对齐后端 AgentConfig) */
export interface AgentDefinition {
  name: string;
  display_name: string;
  description: string;
  model: string;
  temperature: number;
  max_tokens: number;
  tool_groups: string[];
  skills?: string[] | null;
  memory: AgentMemoryConfig;
  features: AgentFeaturesConfig;
  limits: { max_turns: number; timeout_seconds: number };
  team: { can_delegate: boolean; memory_scope: string };
  subagents: { timeout_seconds: number; max_concurrent: number };
  created_at?: string;
  updated_at?: string;
}

/** 后端 agents API 直接返回的 agent 对象 */
export interface AgentListItem {
  name: string;
  display_name: string;
  description: string;
  model: string;
  tool_groups: string[];
  skills?: string[] | null;
  team?: { can_delegate: boolean; memory_scope: string };
  created_at: string;
  updated_at: string;
}

// ===== 监控 =====
export interface TraceSpan {
  id: string;
  name: string;
  type: "trace" | "span" | "generation";
  parentId: string | null;
  startTime: string;
  endTime: string | null;
  durationMs: number;
  metadata: Record<string, unknown>;
  children: TraceSpan[];
}

export interface TokenUsageStats {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  by_model: Record<string, TokenUsage>;
  by_date: Array<{ date: string; tokens: number; cost: number }>;
}

// ===== Agent 对话日志 (对齐后端 agent_logs.py JSONL 格式) =====

export interface AgentLogEntry {
  type: "message" | "task_boundary";
  role?: "human" | "ai" | "tool_call" | "tool_result";
  content?: string;
  tool_name?: string | null;
  task_id: string;
  title?: string; // task_boundary
  status?: string; // task_boundary
  summary?: string; // task_boundary
  timestamp: string;
}

export interface AgentLogInfo {
  agent_name: string;
  task_count: number;
  entry_count: number;
  size_bytes: number;
  error?: string;
}

// ===== 定时任务 (对齐后端 app/models/scheduled_task.py) =====

export interface ScheduledTask {
  id: string;
  user_id: string;
  name: string;
  prompt: string;
  cron_expr: string | null;
  recurring: boolean;
  timezone: string;
  next_run_at: string | null;
  expires_at: string | null;
  enabled: boolean;
  mode: "single" | "team";
  project_id: string | null;
  agent_name: string | null;
  thread_strategy: "new" | "fixed";
  thread_id: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  created_by: "user" | "agent";
  allow_silent: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskRun {
  id: string;
  task_id: string;
  thread_id: string | null;
  status: "running" | "success" | "error" | "timeout" | "interrupted";
  started_at: string;
  finished_at: string | null;
  error: string | null;
  summary: string | null;
}
