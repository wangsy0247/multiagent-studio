/**
 * API 客户端 — Axios 封装，JWT 自动注入
 */

import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";

const API_BASE = "/api";

const apiClient = axios.create({
  baseURL: API_BASE,
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// 请求拦截器 — 注入 JWT
apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (typeof window !== "undefined") {
    const stored = localStorage.getItem("auth-storage");
    if (stored) {
      try {
        const { state } = JSON.parse(stored);
        const token = state?.accessToken;
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }
      } catch {}
    }
  }
  return config;
});

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ detail: string }>) => {
    if (error.response?.status === 401) {
      if (typeof window !== "undefined") {
        localStorage.removeItem("auth-storage");
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

export default apiClient;

/** 从 localStorage 读取 JWT (与 axios 请求拦截器同源) */
export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = localStorage.getItem("auth-storage");
    if (stored) {
      const { state } = JSON.parse(stored);
      return state?.accessToken ?? null;
    }
  } catch {}
  return null;
}

/**
 * 带 JWT 的裸 fetch — 给 axios 之外的访问用 (文件预览/下载)。
 * 文件端点走 HTTPBearer 鉴权, 裸 fetch / <a href> / <img src> 不带请求头会 401。
 */
export async function authFetch(url: string): Promise<Response> {
  const token = getAccessToken();
  return fetch(
    url,
    token ? { headers: { Authorization: `Bearer ${token}` } } : undefined,
  );
}

/** 带鉴权拉取文件并生成本地 objectURL (供 <img>/<iframe> 等无法自定义请求头的场景) */
export async function fetchFileObjectUrl(url: string): Promise<string> {
  const r = await authFetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return URL.createObjectURL(await r.blob());
}

/** 带鉴权下载文件到本地 (创建临时 a[download] 触发浏览器保存) */
export async function downloadWithAuth(url: string, filename: string): Promise<void> {
  const objUrl = await fetchFileObjectUrl(url);
  const a = document.createElement("a");
  a.href = objUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objUrl), 10_000);
}

/** 获取当前登录用户的文件系统 ID — 统一为 username（目录 ~/.multiagent-studio/users/{username}/），兜底 id，再兜底 "default" */
export function getCurrentUserId(): string {
  if (typeof window === "undefined") return "default";
  try {
    const stored = localStorage.getItem("auth-storage");
    if (stored) {
      const { state } = JSON.parse(stored);
      const uid = state?.user?.username || state?.user?.id;
      if (uid) return uid;
    }
  } catch {}
  return "default";
}

export const authAPI = {
  register: (data: { email: string; username: string; password: string }) =>
    apiClient.post("/auth/register", data),
  login: (data: { email: string; password: string }) =>
    apiClient.post("/auth/login", data),
  getMe: () => apiClient.get("/auth/me"),
  updateMe: (data: { display_name?: string }) =>
    apiClient.put("/auth/me", data),
};

export const executeAPI = {
  stop: (threadId: string) => apiClient.post(`/execute/${threadId}/stop`),
  getStatus: (threadId: string) =>
    apiClient.get<{ thread_id: string; status: string }>(`/execute/${threadId}/status`),
};

export const threadsAPI = {
  list: (page = 1) => apiClient.get(`/threads?page=${page}`),
  create: (data: {
    title?: string; preset_type?: string;
    project_id?: string; agent_name?: string; mode?: string;
  }) => apiClient.post("/threads", data),
  get: (threadId: string) => apiClient.get(`/threads/${threadId}`),
  delete: (threadId: string) => apiClient.delete(`/threads/${threadId}`),
  updateTitle: (threadId: string, title: string) =>
    apiClient.patch(`/threads/${threadId}/title`, { title }),
  updateGraph: (threadId: string, graph: object) =>
    apiClient.patch(`/threads/${threadId}/graph`, { execution_graph: graph }),
  getMessages: (threadId: string, page = 1) =>
    apiClient.get(`/threads/${threadId}/messages?page=${page}`),
  // ── Agent Team: 项目内线程 ──
  listByProject: (projectId: string) =>
    apiClient.get(`/threads/by-project/${projectId}`),
};

export const filesAPI = {
  upload: (threadId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return apiClient.post(`/files/upload?thread_id=${threadId}`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
  },
  list: (threadId?: string) =>
    apiClient.get(`/files${threadId ? `?thread_id=${threadId}` : ""}`),
  download: (fileId: string) => apiClient.get(`/files/${fileId}`, { responseType: "blob" }),
  delete: (fileId: string) => apiClient.delete(`/files/${fileId}`),
  // ── outputs 产物 (agent 交付物 artifacts) ──
  listOutputs: (threadId: string) => apiClient.get(`/files/outputs/${threadId}`),
  /**
   * 构造 outputs 产物的下载/预览 URL。
   * `path` 接受虚拟路径 (/mnt/user-data/outputs/...) 或 outputs 下的相对路径;
   * 逐段 encodeURIComponent, 支持中文文件名。
   */
  outputsUrl: (threadId: string, path: string, download = false) => {
    const rel = path.replace(/^\/mnt\/user-data\/outputs\//, "").replace(/^\/+/, "");
    const encoded = rel.split("/").map(encodeURIComponent).join("/");
    return `/api/files/outputs/${threadId}/${encoded}${download ? "?download=true" : ""}`;
  },
};

/**
 * markdown 链接/图片路径映射: `/mnt/user-data/outputs/...` → outputs 端点 URL。
 * 其他路径 (含 uploads/workspace 虚拟路径) 返回 null, 保持原样。
 */
export function resolveOutputsUrl(
  threadId: string | null | undefined,
  href: string,
  download = false
): string | null {
  if (!threadId || !href.startsWith("/mnt/user-data/outputs/")) return null;
  return filesAPI.outputsUrl(threadId, href, download);
}

// ===== Agents API (persistent per-user agents) =====
export const agentsAPI = {
  list: () => apiClient.get(`/v1/agents?user_id=${getCurrentUserId()}`),
  get: (name: string) => apiClient.get(`/v1/agents/${name}?user_id=${getCurrentUserId()}`),
  create: (data: {
    name: string;  // 模型由服务器统一配置, 无需 model 字段
    display_name?: string; description?: string;
    soul?: string; tool_groups?: string[];
    skills?: string[]; user_id?: string;
    temperature?: number; max_tokens?: number;
    memory?: { backend?: string; max_facts?: number; injection_enabled?: boolean; };
    features?: { summarization?: boolean; subagent?: boolean; langfuse?: boolean; };
    limits?: { max_turns?: number; timeout_seconds?: number; };
    team?: { can_delegate?: boolean; memory_scope?: string; };
    mcp_servers?: Record<string, boolean>;      // per-agent MCP 黑名单 (false=禁用)
    skills_enabled?: Record<string, boolean>;   // per-agent skill 黑名单 (false=禁用)
  }) => apiClient.post("/v1/agents", { ...data, user_id: data.user_id || getCurrentUserId() }),
  update: (name: string, data: object) => apiClient.put(`/v1/agents/${name}`, { ...data, user_id: getCurrentUserId() }),
  delete: (name: string) => apiClient.delete(`/v1/agents/${name}?user_id=${getCurrentUserId()}`),
  getMemory: (name: string) => apiClient.get(`/v1/agents/${name}/memory?user_id=${getCurrentUserId()}`),
  clearMemory: (name: string) => apiClient.delete(`/v1/agents/${name}/memory?user_id=${getCurrentUserId()}`),
};

// ===== Extensions API (MCP 服务 + 技能, 代理到 harness) =====
export interface McpServerConfig {
  enabled: boolean;
  type: "stdio" | "http" | "sse";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  description?: string;
}

export interface SkillSummary {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
  allowed_tools?: string[] | null;
  license?: string | null;
  user_id?: string | null;  // 非空 = 用户私有 skill
}

export const extensionsAPI = {
  // ── MCP servers (全局) ──
  listMcpServers: () => apiClient.get("/extensions/mcp/servers"),
  upsertMcpServer: (name: string, data: McpServerConfig) =>
    apiClient.put(`/extensions/mcp/servers/${name}`, data),
  setMcpServerEnabled: (name: string, enabled: boolean) =>
    apiClient.put(`/extensions/mcp/servers/${name}/enabled`, { enabled }),
  deleteMcpServer: (name: string) => apiClient.delete(`/extensions/mcp/servers/${name}`),
  // ── Skills ──
  listSkills: () => apiClient.get("/extensions/skills"),
  listAgentSkills: () => apiClient.get("/extensions/skills/agent-skills"),
  toggleSkill: (name: string, enabled: boolean) =>
    apiClient.put(`/extensions/skills/${name}/enabled`, { enabled }),
  getCustomSkill: (name: string) => apiClient.get(`/extensions/skills/custom/${name}`),
  writeCustomSkill: (name: string, content: string) =>
    apiClient.put(`/extensions/skills/custom/${name}`, { content }),
  deleteCustomSkill: (name: string) => apiClient.delete(`/extensions/skills/custom/${name}`),
  installSkillFromUrl: (url: string, force = false) =>
    apiClient.post("/extensions/skills/install", { url, force }),
};

// ===== Projects API =====
export const projectsAPI = {
  list: () => apiClient.get(`/v1/projects?user_id=${getCurrentUserId()}`),
  get: (id: string) => apiClient.get(`/v1/projects/${id}?user_id=${getCurrentUserId()}`),
  create: (data: { name: string; description?: string; user_id?: string }) =>
    apiClient.post("/v1/projects", { ...data, user_id: data.user_id || getCurrentUserId() }),
  update: (id: string, data: object) => apiClient.put(`/v1/projects/${id}`, { ...data, user_id: getCurrentUserId() }),
  delete: (id: string) => apiClient.delete(`/v1/projects/${id}?user_id=${getCurrentUserId()}`),
  addMember: (id: string, agentName: string) =>
    apiClient.post(`/v1/projects/${id}/members`, { agent_name: agentName, user_id: getCurrentUserId() }),
  removeMember: (id: string, agentName: string) =>
    apiClient.delete(`/v1/projects/${id}/members/${agentName}?user_id=${getCurrentUserId()}`),
  getAgentCards: (id: string) =>
    apiClient.get(`/v1/projects/${id}/agent-cards?user_id=${getCurrentUserId()}`),
  // Tasks (thread_id 为空时返回未关联 thread 的任务)
  listTasks: (id: string, threadId?: string) => apiClient.get(`/v1/projects/${id}/tasks?user_id=${getCurrentUserId()}&thread_id=${threadId || ""}`),
  createTask: (id: string, data: object, threadId?: string) => apiClient.post(`/v1/projects/${id}/tasks`, { ...data, user_id: getCurrentUserId(), thread_id: threadId || "" }),
  updateTask: (id: string, taskId: string, data: object, threadId?: string) => apiClient.put(`/v1/projects/${id}/tasks/${taskId}`, { ...data, user_id: getCurrentUserId(), thread_id: threadId || "" }),
  deleteTask: (id: string, taskId: string, threadId?: string) => apiClient.delete(`/v1/projects/${id}/tasks/${taskId}?user_id=${getCurrentUserId()}&thread_id=${threadId || ""}`),
  // Agent 对话日志 (按 agent 隔离展示工作内容)
  getAgentLogs: (projectId: string, threadId: string) =>
    apiClient.get(`/v1/projects/${projectId}/agent-logs/${threadId}?user_id=${getCurrentUserId()}`),
  getAgentLog: (projectId: string, threadId: string, agentName: string) =>
    apiClient.get(`/v1/projects/${projectId}/agent-logs/${threadId}/${agentName}?user_id=${getCurrentUserId()}`),
};

export const configsAPI = {
  get: () => apiClient.get("/configs"),
  update: (data: object) => apiClient.put("/configs", data),
  getPresets: () => apiClient.get("/configs/presets"),
  getToolGroups: () => apiClient.get("/configs/tool-groups"),
};

export const monitoringAPI = {
  getTrace: (threadId: string) => apiClient.get(`/monitoring/traces/${threadId}`),
  /** thread_id 可选: 带则为当前会话统计, 不带为全部会话 */
  getTokenUsage: (params?: { thread_id?: string }) =>
    apiClient.get("/monitoring/token-usage", { params }),
};

// ===== Scheduled Tasks API (定时任务) =====
export const scheduledTasksAPI = {
  list: () => apiClient.get("/scheduled-tasks"),
  create: (data: object) => apiClient.post("/scheduled-tasks", data),
  update: (id: string, data: object) => apiClient.patch(`/scheduled-tasks/${id}`, data),
  delete: (id: string) => apiClient.delete(`/scheduled-tasks/${id}`),
  trigger: (id: string) => apiClient.post(`/scheduled-tasks/${id}/trigger`),
  listRuns: (id: string, limit = 50) =>
    apiClient.get(`/scheduled-tasks/${id}/runs?limit=${limit}`),
  markRunsSeen: (id: string) => apiClient.post(`/scheduled-tasks/${id}/runs/mark-seen`),
  unreadCount: () => apiClient.get("/scheduled-tasks/unread-count"),
  preview: (cronExpr: string, timezone: string, count = 5) =>
    apiClient.get("/scheduled-tasks/preview", { params: { cron_expr: cronExpr, timezone, count } }),
};
