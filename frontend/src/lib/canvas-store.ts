/**
 * 画布状态管理 — React Flow 节点/边 + 序列化
 */

import { create } from "zustand";
import {
  Node,
  Edge,
  addEdge,
  Connection,
  applyNodeChanges,
  applyEdgeChanges,
  NodeChange,
  EdgeChange,
} from "reactflow";
import { SubAgentConfig, ExecutionGraph, CanvasNode } from "./types";
import { generateId } from "./utils";

// 默认 Lead Agent 配置
const DEFAULT_LEAD_CONFIG: SubAgentConfig = {
  name: "lead",
  display_name: "Lead Agent",
  description: "主编排Agent",
  system_prompt: "你是多Agent系统的主编排器...",
  model: "inherit",
  tools: null,
  disallowed_tools: [],
  temperature: 0.3,
  max_turns: 20,
};

// 默认 SubAgent 配置
const DEFAULT_SUB_CONFIG: SubAgentConfig = {
  name: "",
  display_name: "新 SubAgent",
  description: "",
  system_prompt: "",
  model: "inherit",
  tools: null,
  disallowed_tools: [],
  temperature: 0.3,
  max_turns: 10,
};

interface CanvasStore {
  nodes: Node<CanvasNode["data"]>[];
  edges: Edge[];
  selectedNodeId: string | null;
  validationErrors: string[];

  // 节点操作
  onNodesChange: (changes: NodeChange[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  addNode: (type: "lead" | "subagent", position: { x: number; y: number }) => void;
  removeNode: (nodeId: string) => void;
  selectNode: (nodeId: string | null) => void;
  updateNodeConfig: (nodeId: string, config: Partial<SubAgentConfig>) => void;

  // 状态更新 (来自 SSE)
  setNodeStatus: (nodeId: string, status: CanvasNode["data"]["status"]) => void;

  // 画布操作
  clearCanvas: () => void;
  exportGraph: () => ExecutionGraph;
  importGraph: (graph: ExecutionGraph) => void;
  validate: () => boolean;
}

let nodeCounter = 0;

function createNode(type: "lead" | "subagent", position: { x: number; y: number }): Node<CanvasNode["data"]> {
  const id = `${type}_${++nodeCounter}_${Date.now()}`;
  const config = type === "lead" ? { ...DEFAULT_LEAD_CONFIG } : { ...DEFAULT_SUB_CONFIG };
  return {
    id,
    type: "agentNode",
    position,
    data: { config, status: "idle", isEntryPoint: type === "lead" },
  };
}

export const useCanvasStore = create<CanvasStore>((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  validationErrors: [],

  onNodesChange: (changes) =>
    set((s) => ({ nodes: applyNodeChanges(changes, s.nodes) })),

  onEdgesChange: (changes) =>
    set((s) => ({ edges: applyEdgeChanges(changes, s.edges) })),

  onConnect: (connection) =>
    set((s) => ({ edges: addEdge({ ...connection, type: "smoothstep" }, s.edges) })),

  addNode: (type, position) => {
    // Lead Agent 只允许一个
    if (type === "lead" && get().nodes.some((n) => n.data.isEntryPoint)) {
      return;
    }
    const node = createNode(type, position);
    set((s) => ({ nodes: [...s.nodes, node] }));
  },

  removeNode: (nodeId) =>
    set((s) => ({
      nodes: s.nodes.filter((n) => n.id !== nodeId),
      edges: s.edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      selectedNodeId: s.selectedNodeId === nodeId ? null : s.selectedNodeId,
    })),

  selectNode: (nodeId) => set({ selectedNodeId: nodeId }),

  updateNodeConfig: (nodeId, partial) =>
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === nodeId
          ? { ...n, data: { ...n.data, config: { ...n.data.config, ...partial } } }
          : n
      ),
    })),

  setNodeStatus: (nodeId, status) =>
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === nodeId ? { ...n, data: { ...n.data, status } } : n
      ),
    })),

  clearCanvas: () => set({ nodes: [], edges: [], selectedNodeId: null, validationErrors: [] }),

  exportGraph: () => {
    const { nodes, edges } = get();
    const entryPoint = nodes.find((n) => n.data.isEntryPoint);
    return {
      nodes: nodes.map((n) => ({
        id: n.id,
        type: n.data.isEntryPoint ? "lead" : "subagent",
        config: n.data.config,
        position: n.position,
        connections: edges
          .filter((e) => e.source === n.id)
          .map((e) => e.target),
      })),
      edges: edges.map((e) => [e.source, e.target] as [string, string]),
      entry_point: entryPoint?.id || "",
    };
  },

  importGraph: (graph) => {
    const nodes: Node<CanvasNode["data"]>[] = graph.nodes.map((n) => ({
      id: n.id,
      type: "agentNode",
      position: n.position,
      data: {
        config: n.config,
        status: "idle",
        isEntryPoint: n.id === graph.entry_point || n.type === "lead",
      },
    }));
    const edges: Edge[] = graph.edges.map(([source, target]) => ({
      id: `${source}_${target}`,
      source,
      target,
      type: "smoothstep",
    }));
    set({ nodes, edges, selectedNodeId: null, validationErrors: [] });
  },

  validate: () => {
    const { nodes, edges } = get();
    const errors: string[] = [];

    // 必须有至少一个 Lead Agent
    const leadNodes = nodes.filter((n) => n.data.isEntryPoint);
    if (leadNodes.length === 0) errors.push("需要至少一个 Lead Agent 节点");
    if (leadNodes.length > 1) errors.push("只能有一个 Lead Agent 节点");

    // 节点名唯一
    const names = nodes.map((n) => n.data.config.name).filter(Boolean);
    const dupes = names.filter((n, i) => names.indexOf(n) !== i);
    if (dupes.length > 0) errors.push(`节点名称重复: ${[...new Set(dupes)].join(", ")}`);

    // 无环检测
    const adj = new Map<string, string[]>();
    nodes.forEach((n) => adj.set(n.id, []));
    edges.forEach((e) => adj.get(e.source)?.push(e.target));

    const visited = new Set<string>();
    const recStack = new Set<string>();
    function hasCycle(u: string): boolean {
      visited.add(u);
      recStack.add(u);
      for (const v of adj.get(u) || []) {
        if (!visited.has(v) && hasCycle(v)) return true;
        if (recStack.has(v)) return true;
      }
      recStack.delete(u);
      return false;
    }
    for (const n of nodes) {
      if (!visited.has(n.id) && hasCycle(n.id)) {
        errors.push("图中存在环路");
        break;
      }
    }

    // SubAgent 节点必须有名称
    const unnamed = nodes.filter(
      (n) => !n.data.isEntryPoint && !n.data.config.name
    );
    if (unnamed.length > 0) errors.push(`有 ${unnamed.length} 个节点未命名`);

    set({ validationErrors: errors });
    return errors.length === 0;
  },
}));
