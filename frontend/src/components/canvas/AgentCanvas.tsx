"use client";

import { useCallback, useRef, useState, useEffect } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Panel,
  ReactFlowProvider,
  ReactFlowInstance,
} from "reactflow";
import "reactflow/dist/style.css";

import { useCanvasStore } from "@/lib/canvas-store";
import { ExecutionGraph } from "@/lib/types";
import { threadsAPI } from "@/lib/api-client";
import AgentNodeComponent from "./AgentNode";
import NodePalette from "./NodePalette";
import ConfigPanel from "./ConfigPanel";
import CanvasControls from "./CanvasControls";

// 注册自定义节点类型
const nodeTypes = { agentNode: AgentNodeComponent };

interface AgentCanvasProps {
  threadId: string;
  initialGraph: ExecutionGraph | null;
}

export default function AgentCanvas({ threadId, initialGraph }: AgentCanvasProps) {
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [reactFlowInstance, setReactFlowInstance] = useState<ReactFlowInstance | null>(null);
  const [showConfig, setShowConfig] = useState(false);

  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    onConnect,
    addNode,
    selectNode,
    selectedNodeId,
    importGraph,
    validationErrors,
  } = useCanvasStore();

  // 加载初始图
  useEffect(() => {
    if (initialGraph) {
      importGraph(initialGraph);
    }
  }, [initialGraph]);

  // 拖拽放置
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const type = event.dataTransfer.getData("application/reactflow-type") as "lead" | "subagent";
      const presetKey = event.dataTransfer.getData("application/reactflow-preset") || "";
      if (!type || !reactFlowInstance) return;

      const bounds = reactFlowWrapper.current?.getBoundingClientRect();
      const position = reactFlowInstance.project({
        x: event.clientX - (bounds?.left || 0),
        y: event.clientY - (bounds?.top || 0),
      });
      addNode(type, position, presetKey || undefined);
    },
    [reactFlowInstance, addNode]
  );

  // 点击节点选中
  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: any) => {
      selectNode(node.id);
      setShowConfig(true);
    },
    [selectNode]
  );

  // 点击空白取消选中
  const onPaneClick = useCallback(() => {
    selectNode(null);
    setShowConfig(false);
  }, [selectNode]);

  // 保存图
  const saveGraph = useCallback(async () => {
    const graph = useCanvasStore.getState().exportGraph();
    try {
      await threadsAPI.updateGraph(threadId, graph);
    } catch (err) {
      console.error("保存画布失败", err);
    }
  }, [threadId]);

  const selectedNode = nodes.find((n) => n.id === selectedNodeId);

  return (
    <div className="flex h-full">
      <div className="flex-1 relative" ref={reactFlowWrapper}>
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onInit={setReactFlowInstance}
            onDrop={onDrop}
            onDragOver={onDragOver}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            nodeTypes={nodeTypes}
            defaultEdgeOptions={{ type: "smoothstep", animated: true }}
            fitView
            deleteKeyCode={["Backspace", "Delete"]}
            className="bg-muted/20"
          >
            <Background gap={16} color="#e5e7eb" />
            <Controls />
            <MiniMap
              nodeColor={(n) => (n.data?.isEntryPoint ? "#3b82f6" : "#10b981")}
              maskColor="rgba(0,0,0,0.08)"
            />
            <Panel position="top-right">
              <CanvasControls threadId={threadId} />
            </Panel>
          </ReactFlow>
        </ReactFlowProvider>
      </div>

      {/* 左侧节点面板 */}
      <NodePalette />

      {/* 右侧配置面板 */}
      {showConfig && selectedNode && (
        <ConfigPanel
          nodeId={selectedNode.id}
          config={selectedNode.data.config}
          isEntryPoint={selectedNode.data.isEntryPoint}
          onClose={() => {
            setShowConfig(false);
            selectNode(null);
          }}
        />
      )}
    </div>
  );
}
