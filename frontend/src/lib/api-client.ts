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
  create: (data: { title?: string; preset_type?: string }) =>
    apiClient.post("/threads", data),
  get: (threadId: string) => apiClient.get(`/threads/${threadId}`),
  delete: (threadId: string) => apiClient.delete(`/threads/${threadId}`),
  updateTitle: (threadId: string, title: string) =>
    apiClient.patch(`/threads/${threadId}/title`, { title }),
  getMessages: (threadId: string, page = 1) =>
    apiClient.get(`/threads/${threadId}/messages?page=${page}`),
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
  list: (userId = "default") => apiClient.get(`/v1/agents?user_id=${userId}`),
  get: (name: string, userId = "default") => apiClient.get(`/v1/agents/${name}?user_id=${userId}`),
  create: (data: {
    name: string; display_name?: string; description?: string;
    soul?: string; model?: string; tool_groups?: string[];
    skills?: string[]; user_id?: string;
  }) => apiClient.post("/v1/agents", data),
  update: (name: string, data: object) => apiClient.put(`/v1/agents/${name}`, data),
  delete: (name: string, userId = "default") => apiClient.delete(`/v1/agents/${name}?user_id=${userId}`),
  getMemory: (name: string, userId = "default") => apiClient.get(`/v1/agents/${name}/memory?user_id=${userId}`),
  clearMemory: (name: string, userId = "default") => apiClient.delete(`/v1/agents/${name}/memory?user_id=${userId}`),
};

// ===== Projects API =====
export const projectsAPI = {
  list: (userId = "default") => apiClient.get(`/v1/projects?user_id=${userId}`),
  get: (id: string, userId = "default") => apiClient.get(`/v1/projects/${id}?user_id=${userId}`),
  create: (data: { name: string; description?: string; user_id?: string }) =>
    apiClient.post("/v1/projects", data),
  update: (id: string, data: object) => apiClient.put(`/v1/projects/${id}`, data),
  delete: (id: string, userId = "default") => apiClient.delete(`/v1/projects/${id}?user_id=${userId}`),
  addMember: (id: string, agentName: string, userId = "default") =>
    apiClient.post(`/v1/projects/${id}/members`, { agent_name: agentName, user_id: userId }),
  removeMember: (id: string, agentName: string, userId = "default") =>
    apiClient.delete(`/v1/projects/${id}/members/${agentName}?user_id=${userId}`),
  // Tasks
  listTasks: (id: string, userId = "default") => apiClient.get(`/v1/projects/${id}/tasks?user_id=${userId}`),
  createTask: (id: string, data: object) => apiClient.post(`/v1/projects/${id}/tasks`, data),
  updateTask: (id: string, taskId: string, data: object) => apiClient.put(`/v1/projects/${id}/tasks/${taskId}`, data),
  deleteTask: (id: string, taskId: string, userId = "default") => apiClient.delete(`/v1/projects/${id}/tasks/${taskId}?user_id=${userId}`),
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
  getRunEvents: (threadId: string, runId?: string, eventTypes?: string, limit = 100) =>
    apiClient.get(`/v1/runs/${threadId}/events`, { params: { run_id: runId, event_types: eventTypes, limit } }),
};
