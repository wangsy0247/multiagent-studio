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

/** 获取当前登录用户的 ID，优先使用 username（可读性更好），兜底 id，再兜底 "default" */
function getCurrentUserId(): string {
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
};

// ===== Agents API (persistent per-user agents) =====
export const agentsAPI = {
  list: () => apiClient.get(`/v1/agents?user_id=${getCurrentUserId()}`),
  get: (name: string) => apiClient.get(`/v1/agents/${name}?user_id=${getCurrentUserId()}`),
  create: (data: {
    name: string; model: string;  // model 必选
    display_name?: string; description?: string;
    soul?: string; tool_groups?: string[];
    skills?: string[]; user_id?: string;
    temperature?: number; max_tokens?: number;
    memory?: { backend?: string; max_facts?: number; injection_enabled?: boolean; };
    features?: { summarization?: boolean; subagent?: boolean; langfuse?: boolean; };
    limits?: { max_turns?: number; timeout_seconds?: number; };
    team?: { can_be_lead?: boolean; can_delegate?: boolean; memory_scope?: string; };
    mcp_servers?: Record<string, boolean>;
  }) => apiClient.post("/v1/agents", { ...data, user_id: data.user_id || getCurrentUserId() }),
  update: (name: string, data: object) => apiClient.put(`/v1/agents/${name}`, { ...data, user_id: getCurrentUserId() }),
  delete: (name: string) => apiClient.delete(`/v1/agents/${name}?user_id=${getCurrentUserId()}`),
  getMemory: (name: string) => apiClient.get(`/v1/agents/${name}/memory?user_id=${getCurrentUserId()}`),
  clearMemory: (name: string) => apiClient.delete(`/v1/agents/${name}/memory?user_id=${getCurrentUserId()}`),
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
  // Tasks
  listTasks: (id: string) => apiClient.get(`/v1/projects/${id}/tasks?user_id=${getCurrentUserId()}`),
  createTask: (id: string, data: object) => apiClient.post(`/v1/projects/${id}/tasks`, { ...data, user_id: getCurrentUserId() }),
  updateTask: (id: string, taskId: string, data: object) => apiClient.put(`/v1/projects/${id}/tasks/${taskId}`, { ...data, user_id: getCurrentUserId() }),
  deleteTask: (id: string, taskId: string) => apiClient.delete(`/v1/projects/${id}/tasks/${taskId}?user_id=${getCurrentUserId()}`),
};

export const configsAPI = {
  get: () => apiClient.get("/configs"),
  update: (data: object) => apiClient.put("/configs", data),
  getPresets: () => apiClient.get("/configs/presets"),
  getToolGroups: () => apiClient.get("/configs/tool-groups"),
};

export const monitoringAPI = {
  getTrace: (threadId: string) => apiClient.get(`/monitoring/traces/${threadId}`),
  getTokenUsage: (params?: object) => apiClient.get("/monitoring/token-usage", { params }),
};
