/**
 * 项目状态管理 — 项目列表、当前项目、项目线程、项目 Agent
 */
import { create } from "zustand";
import { projectsAPI, threadsAPI, agentsAPI } from "./api-client";
import type { Project, ThreadSummary, AgentDefinition } from "./types";

interface ProjectState {
  projects: Project[];
  currentProject: Project | null;
  projectThreads: ThreadSummary[];
  projectAgents: AgentDefinition[];
  isLoading: boolean;
  error: string | null;

  fetchProjects: () => Promise<void>;
  fetchProject: (id: string) => Promise<void>;
  createProject: (data: Partial<Project>) => Promise<Project>;
  updateProject: (id: string, data: Partial<Project>) => Promise<void>;
  deleteProject: (id: string) => Promise<void>;
  addMember: (projectId: string, agentName: string) => Promise<void>;
  removeMember: (projectId: string, agentName: string) => Promise<void>;
  fetchProjectThreads: (projectId: string) => Promise<void>;
  createProjectThread: (
    projectId: string,
    data: { title?: string; agent_name?: string; mode?: "single" | "team" }
  ) => Promise<ThreadSummary>;
  fetchProjectAgents: (memberNames: string[]) => Promise<void>;
  clearCurrentProject: () => void;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  projects: [],
  currentProject: null,
  projectThreads: [],
  projectAgents: [],
  isLoading: false,
  error: null,

  fetchProjects: async () => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await projectsAPI.list();
      set({ projects: data.projects || [], isLoading: false });
    } catch (err: any) {
      set({ error: err.message || "加载项目失败", isLoading: false });
    }
  },

  fetchProject: async (id) => {
    set({ isLoading: true, error: null });
    try {
      const { data } = await projectsAPI.get(id);
      set({ currentProject: data, isLoading: false });
      // 同时加载线程和 Agent 详情
      get().fetchProjectThreads(id);
      get().fetchProjectAgents(data.members || []);
    } catch (err: any) {
      set({ error: err.message || "加载项目失败", isLoading: false });
    }
  },

  createProject: async (data) => {
    const resp = await projectsAPI.create({
      name: data.name || "新项目",
      description: data.description,
    });
    const created = resp.data;
    set((state) => ({ projects: [created, ...state.projects] }));
    return created;
  },

  updateProject: async (id, data) => {
    const resp = await projectsAPI.update(id, data);
    const updated = resp.data;
    set((state) => ({
      projects: state.projects.map((p) => (p.id === id ? updated : p)),
      currentProject: state.currentProject?.id === id ? updated : state.currentProject,
    }));
  },

  deleteProject: async (id) => {
    await projectsAPI.delete(id);
    set((state) => ({
      projects: state.projects.filter((p) => p.id !== id),
      currentProject: state.currentProject?.id === id ? null : state.currentProject,
    }));
  },

  addMember: async (projectId, agentName) => {
    await projectsAPI.addMember(projectId, agentName);
    await get().fetchProject(projectId);
  },

  removeMember: async (projectId, agentName) => {
    await projectsAPI.removeMember(projectId, agentName);
    await get().fetchProject(projectId);
  },

  fetchProjectThreads: async (projectId) => {
    try {
      const { data } = await threadsAPI.listByProject(projectId);
      // 后端返回 { threads, total, page, page_size }
      set({ projectThreads: data.threads || data || [] });
    } catch (err: any) {
      // 如果是 404，说明后端路由尚未生效，静默处理
      console.warn("listByProject 不可用:", err.message);
      set({ projectThreads: [] });
    }
  },

  createProjectThread: async (projectId, data) => {
    const { data: created } = await threadsAPI.create({
      title: data.title || "新会话",
      project_id: projectId,
      agent_name: data.agent_name,
      mode: data.mode || (projectId ? "team" : "single"),
    });
    set((state) => ({
      projectThreads: [created, ...state.projectThreads],
    }));
    return created;
  },

  fetchProjectAgents: async (memberNames) => {
    try {
      const { data } = await agentsAPI.list();
      const allAgents: AgentDefinition[] = data.agents || [];
      const projectAgents = allAgents.filter((a) => memberNames.includes(a.name));
      set({ projectAgents });
    } catch (err: any) {
      set({ error: err.message || "加载 Agent 失败" });
    }
  },

  clearCurrentProject: () => {
    set({ currentProject: null, projectThreads: [], projectAgents: [] });
  },
}));
