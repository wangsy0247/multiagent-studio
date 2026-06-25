/**
 * 认证状态管理 (Zustand + localStorage 持久化)
 */

import { create } from "zustand";
import { User, AuthState } from "./types";
import { authAPI } from "./api-client";

interface AuthStore extends AuthState {
  isLoading: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, username: string, password: string) => Promise<void>;
  logout: () => void;
  fetchMe: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  user: null,
  accessToken: null,
  isAuthenticated: false,
  isLoading: true,
  error: null,

  login: async (email, password) => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await authAPI.login({ email, password });
      set({
        accessToken: data.access_token,
        isAuthenticated: true,
        isLoading: false,
      });
      // 获取用户信息
      await get().fetchMe();
    } catch (err: any) {
      set({
        isLoading: false,
        error: err.response?.data?.detail || "登录失败",
      });
      throw err;
    }
  },

  register: async (email, username, password) => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await authAPI.register({ email, username, password });
      set({
        accessToken: data.access_token,
        isAuthenticated: true,
        isLoading: false,
      });
      await get().fetchMe();
    } catch (err: any) {
      set({
        isLoading: false,
        error: err.response?.data?.detail || "注册失败",
      });
      throw err;
    }
  },

  logout: () => {
    set({ user: null, accessToken: null, isAuthenticated: false });
  },

  fetchMe: async () => {
    try {
      const { data } = await authAPI.getMe();
      set({ user: data, isAuthenticated: true, isLoading: false });
    } catch {
      set({ user: null, accessToken: null, isAuthenticated: false, isLoading: false });
    }
  },

  clearError: () => set({ error: null }),
}));

// 持久化 token 到 localStorage
if (typeof window !== "undefined") {
  useAuthStore.subscribe((state) => {
    localStorage.setItem(
      "auth-storage",
      JSON.stringify({ state: { accessToken: state.accessToken } })
    );
  });
}
