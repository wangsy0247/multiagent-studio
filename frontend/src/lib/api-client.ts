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

// 响应拦截器 — 统一错误处理
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

// ===== 便捷 API 方法 =====

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
  updateGraph: (threadId: string, execution_graph: object) =>
    apiClient.patch(`/threads/${threadId}/graph`, { execution_graph }),
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
