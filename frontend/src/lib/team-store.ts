/**
 * Team 运行时状态管理 — 成员状态、任务、消息
 */
import { create } from "zustand";
import type {
  ProjectTask,
  TeamMemberRuntime,
  TeamMemberRuntimeStatus,
  TeamMessage,
} from "./types";

interface TeamState {
  isRunning: boolean;
  members: TeamMemberRuntime[];
  tasks: ProjectTask[];
  messages: TeamMessage[];
  currentRound: number;
  maxRounds: number;

  setRunning: (running: boolean) => void;
  initMembers: (agentNames: string[], displayNames?: Record<string, string>) => void;
  updateMemberStatus: (
    agentName: string,
    status: TeamMemberRuntimeStatus,
    taskId?: string,
    taskTitle?: string
  ) => void;
  addTask: (task: ProjectTask) => void;
  updateTask: (task: Partial<ProjectTask> & { id: string }) => void;
  addMessage: (msg: TeamMessage) => void;
  setMaxRounds: (max: number) => void;
  incrementRound: () => void;
  reset: () => void;
}

export const useTeamStore = create<TeamState>((set) => ({
  isRunning: false,
  members: [],
  tasks: [],
  messages: [],
  currentRound: 0,
  maxRounds: 100,

  setRunning: (running) => set({ isRunning: running }),

  initMembers: (agentNames, displayNames) =>
    set({
      members: agentNames.map((name) => ({
        agent_name: name,
        display_name: displayNames?.[name] || name,
        status: "idle",
      })),
    }),

  updateMemberStatus: (agentName, status, taskId, taskTitle) =>
    set((state) => ({
      members: state.members.map((m) =>
        m.agent_name === agentName
          ? {
              ...m,
              status,
              current_task_id: taskId || m.current_task_id,
              current_task_title: taskTitle || m.current_task_title,
            }
          : m
      ),
    })),

  addTask: (task) =>
    set((state) => {
      // 避免重复添加同一任务
      if (state.tasks.some((t) => t.id === task.id)) {
        return {
          tasks: state.tasks.map((t) => (t.id === task.id ? task : t)),
        };
      }
      return { tasks: [task, ...state.tasks] };
    }),

  updateTask: (task) =>
    set((state) => ({
      tasks: state.tasks.map((t) => (t.id === task.id ? { ...t, ...task } : t)),
    })),

  addMessage: (msg) =>
    set((state) => ({
      messages: [...state.messages, msg],
    })),

  setMaxRounds: (max) => set({ maxRounds: max }),

  incrementRound: () =>
    set((state) => ({ currentRound: state.currentRound + 1 })),

  reset: () =>
    set({
      isRunning: false,
      members: [],
      tasks: [],
      messages: [],
      currentRound: 0,
      maxRounds: 100,
    }),
}));
