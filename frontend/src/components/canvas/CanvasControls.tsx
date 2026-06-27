"use client";

import { useState } from "react";
import { Play, FileJson, Upload, Trash2, AlertTriangle, CheckCircle, RotateCcw } from "lucide-react";
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

    let connected = false;
    const sse = new SSEClient({
      onEvent: (event) => { connected = true; handleSSEEvent(event); },
      onStatus: (status) => {
        if (status === "connected") connected = true;
        if (status === "error" && !connected) {
          setStreaming(false);
          useChatStore.getState().setError("无法连接后端服务 (localhost:8000)，请确认已启动 Harness + App 服务");
        }
      },
      maxReconnectAttempts: 1,
    });

    try {
      await sse.connect("/api/execute", {
        thread_id: threadId,
        message,
        execution_graph: graph,
      });
    } catch (err) {
      console.error("执行出错", err);
      if (!connected) {
        useChatStore.getState().setError("无法连接后端服务，请确认端口 8000 已启动");
      }
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
    <div className="relative inline-flex items-center gap-0.5 bg-white border border-slate-200 rounded-xl shadow-sm p-1">
      <button
        onClick={handleRun}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-colors font-medium"
        title="运行"
      >
        <Play className="w-3 h-3" />
        运行
      </button>

      <button
        onClick={() => { validate(); setShowValidation(true); }}
        className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors group"
        title="校验画布"
      >
        <CheckCircle className="w-3.5 h-3.5 text-slate-400 group-hover:text-slate-600" />
      </button>

      <div className="w-px h-6 bg-slate-200 mx-0.5" />

      <button
        onClick={handleSave}
        className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors group"
        title="保存"
      >
        <Upload className="w-3.5 h-3.5 text-slate-400 group-hover:text-slate-600" />
      </button>

      <button onClick={handleExport} className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors group" title="导出 JSON">
        <FileJson className="w-3.5 h-3.5 text-slate-400 group-hover:text-slate-600" />
      </button>

      <button
        onClick={() => setShowImport(!showImport)}
        className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors group"
        title="导入 JSON"
      >
        <RotateCcw className="w-3.5 h-3.5 text-slate-400 group-hover:text-slate-600" />
      </button>

      <div className="w-px h-6 bg-slate-200 mx-0.5" />

      <button
        onClick={clearCanvas}
        className="p-1.5 rounded-lg hover:bg-red-50 transition-colors group"
        title="清空画布"
      >
        <Trash2 className="w-3.5 h-3.5 text-slate-400 group-hover:text-red-500" />
      </button>

      {/* Validation popup */}
      {showValidation && validationErrors.length > 0 && (
        <div className="absolute top-full right-0 mt-2 w-72 bg-white border border-slate-200 rounded-xl shadow-lg p-4 z-50 animate-scale-in">
          <div className="flex items-center gap-2 text-red-600 text-xs font-semibold mb-3">
            <AlertTriangle className="w-4 h-4" />
            校验错误 ({validationErrors.length})
          </div>
          <ul className="space-y-1.5 mb-3">
            {validationErrors.map((e, i) => (
              <li key={i} className="text-xs text-slate-600 pl-1">• {e}</li>
            ))}
          </ul>
          <button
            onClick={() => setShowValidation(false)}
            className="w-full py-1.5 text-xs bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
          >
            关闭
          </button>
        </div>
      )}

      {showValidation && validationErrors.length === 0 && (
        <div className="absolute top-full right-0 mt-2 w-56 bg-white border border-slate-200 rounded-xl shadow-lg p-4 z-50 animate-scale-in">
          <div className="flex items-center gap-2 text-emerald-600 text-xs font-semibold">
            <CheckCircle className="w-4 h-4" />
            校验通过
          </div>
          <button
            onClick={() => setShowValidation(false)}
            className="mt-3 w-full py-1.5 text-xs bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
          >
            关闭
          </button>
        </div>
      )}

      {/* Import popup */}
      {showImport && (
        <div className="absolute top-full right-0 mt-2 w-80 bg-white border border-slate-200 rounded-xl shadow-lg p-4 z-50 animate-scale-in">
          <textarea
            value={importInput}
            onChange={(e) => setImportInput(e.target.value)}
            rows={6}
            className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg font-mono input-focus"
            placeholder='粘贴 ExecutionGraph JSON...'
          />
          <div className="flex gap-2 mt-3">
            <button
              onClick={handleImport}
              className="flex-1 py-2 text-xs bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-colors font-medium"
            >
              导入
            </button>
            <button
              onClick={() => setShowImport(false)}
              className="flex-1 py-2 text-xs bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
