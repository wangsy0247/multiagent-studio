"use client";

import { useState } from "react";
import { Play, Download, Upload, Trash2, AlertTriangle, CheckCircle } from "lucide-react";
import { useCanvasStore } from "@/lib/canvas-store";
import { useChatStore } from "@/lib/chat-store";
import { SSEClient } from "@/lib/sse-client";
import { threadsAPI } from "@/lib/api-client";

interface CanvasControlsProps {
  threadId: string;
}

export default function CanvasControls({ threadId }: CanvasControlsProps) {
  const [importInput, setImportInput] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [showValidation, setShowValidation] = useState(false);
  const { exportGraph, importGraph, validate, validationErrors, clearCanvas } = useCanvasStore();
  const { handleSSEEvent, setStreaming, addMessage } = useChatStore();

  async function handleRun() {
    if (!validate()) {
      setShowValidation(true);
      return;
    }
    setShowValidation(false);
    const graph = exportGraph();

    const message = prompt("请输入任务指令:") || "请执行任务";
    addMessage({ role: "human", content: message, msgType: "text", metadata: {}, tokenCount: 0 });
    setStreaming(true);

    const sse = new SSEClient({
      onEvent: (event) => handleSSEEvent(event),
    });

    try {
      await sse.connect("/api/execute", {
        thread_id: threadId,
        message,
        execution_graph: graph,
      });
    } catch (err) {
      console.error("执行出错", err);
    } finally {
      setStreaming(false);
    }
  }

  function handleExport() {
    const graph = exportGraph();
    const blob = new Blob([JSON.stringify(graph, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "execution_graph.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  function handleImport() {
    try {
      const graph = JSON.parse(importInput);
      importGraph(graph);
      setShowImport(false);
      setImportInput("");
      // 保存到后端
      threadsAPI.updateGraph(threadId, graph).catch(console.error);
    } catch {
      alert("JSON 格式无效");
    }
  }

  async function handleSave() {
    const graph = exportGraph();
    try {
      await threadsAPI.updateGraph(threadId, graph);
    } catch (err) {
      console.error("保存失败", err);
    }
  }

  return (
    <div className="flex items-center gap-1 bg-card border rounded-lg shadow-sm p-1">
      <button
        onClick={handleRun}
        className="flex items-center gap-1 px-2.5 py-1.5 text-xs bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition font-medium"
        title="运行 (先校验再执行)"
      >
        <Play className="w-3 h-3" />
        运行
      </button>

      <button
        onClick={() => validate() && setShowValidation(true)}
        className="p-1.5 rounded hover:bg-accent transition"
        title="校验画布"
      >
        <CheckCircle className="w-3.5 h-3.5 text-muted-foreground" />
      </button>

      <div className="w-px h-6 bg-border mx-1" />

      <button
        onClick={handleSave}
        className="p-1.5 rounded hover:bg-accent transition"
        title="保存到服务器"
      >
        <Download className="w-3.5 h-3.5 text-muted-foreground" />
      </button>

      <button onClick={handleExport} className="p-1.5 rounded hover:bg-accent transition" title="导出 JSON">
        <Upload className="w-3.5 h-3.5 text-muted-foreground" />
      </button>

      <button
        onClick={() => setShowImport(!showImport)}
        className="p-1.5 rounded hover:bg-accent transition"
        title="导入 JSON"
      >
        <Download className="w-3.5 h-3.5 text-muted-foreground rotate-180" />
      </button>

      <div className="w-px h-6 bg-border mx-1" />

      <button
        onClick={clearCanvas}
        className="p-1.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition"
        title="清空画布"
      >
        <Trash2 className="w-3.5 h-3.5" />
      </button>

      {/* 校验错误弹窗 */}
      {showValidation && validationErrors.length > 0 && (
        <div className="absolute top-full right-0 mt-2 w-64 bg-card border rounded-lg shadow-lg p-3 z-50">
          <div className="flex items-center gap-2 text-destructive text-xs font-medium mb-2">
            <AlertTriangle className="w-3.5 h-3.5" />
            校验错误 ({validationErrors.length})
          </div>
          <ul className="space-y-1">
            {validationErrors.map((e, i) => (
              <li key={i} className="text-xs text-muted-foreground">• {e}</li>
            ))}
          </ul>
          <button
            onClick={() => setShowValidation(false)}
            className="mt-2 w-full py-1 text-xs bg-muted rounded hover:bg-accent"
          >
            关闭
          </button>
        </div>
      )}

      {showValidation && validationErrors.length === 0 && (
        <div className="absolute top-full right-0 mt-2 w-48 bg-card border rounded-lg shadow-lg p-3 z-50">
          <div className="flex items-center gap-2 text-green-600 text-xs font-medium">
            <CheckCircle className="w-3.5 h-3.5" />
            校验通过
          </div>
          <button
            onClick={() => setShowValidation(false)}
            className="mt-2 w-full py-1 text-xs bg-muted rounded hover:bg-accent"
          >
            关闭
          </button>
        </div>
      )}

      {/* 导入弹窗 */}
      {showImport && (
        <div className="absolute top-full right-0 mt-2 w-80 bg-card border rounded-lg shadow-lg p-3 z-50">
          <textarea
            value={importInput}
            onChange={(e) => setImportInput(e.target.value)}
            rows={6}
            className="w-full px-2 py-1.5 text-xs border rounded font-mono"
            placeholder='粘贴 ExecutionGraph JSON...'
          />
          <div className="flex gap-2 mt-2">
            <button
              onClick={handleImport}
              className="flex-1 py-1 text-xs bg-primary text-primary-foreground rounded hover:bg-primary/90"
            >
              导入
            </button>
            <button
              onClick={() => setShowImport(false)}
              className="flex-1 py-1 text-xs bg-muted rounded hover:bg-accent"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
