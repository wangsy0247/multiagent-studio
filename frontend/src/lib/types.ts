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
  // ── Agent Team 事件 ──
  | "team_start" | "team_end" | "team_status"
  | "team_task_update" | "team_message" | "member_status"
  | "team_error" | "team_degrade";

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
  id?: string;
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
export interface User {
  id: string;
  email: string;
  username: string;
  displayName: string;
  role: "user" | "admin";
  avatarUrl: string;
  isActive: boolean;
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

export type ProjectTaskStatus =
  | "todo" | "in_progress" | "in_review"
  | "completed" | "failed" | "rejected" | "merged";

export interface ProjectTask {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: ProjectTaskStatus;
  assigned_agent?: string;
  dependencies: string[];
  priority: "low" | "medium" | "high";
  result_summary?: string;
  result_detail?: Record<string, any>;
  created_at: string;
  updated_at: string;
}

export type TeamMemberRuntimeStatus = "idle" | "busy" | "done" | "failed";

export interface TeamMemberRuntime {
  agent_name: string;
  display_name?: string;
  status: TeamMemberRuntimeStatus;
  current_task_id?: string;
  current_task_title?: string;
  last_result?: string;
}

export type TeamMessageType =
  | "task" | "result" | "question" | "answer"
  | "broadcast" | "lifecycle";

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

/** 自定义 Agent 定义 (对齐后端 AgentConfig) */
export interface AgentDefinition {
  name: string;
  display_name: string;
  description: string;
  model: string;
  tool_groups: string[];
  skills?: string[];
  memory_scope: string;
  can_be_lead: boolean;
  can_delegate: boolean;
  max_turns: number;
  timeout_seconds: number;
  isolation: string;
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
