"use client";

import { X, Copy, Check, Zap, Code, Clock, Cpu } from "lucide-react";
import { useState, useCallback } from "react";
import { SubAgentConfig } from "@/lib/types";
import { useCanvasStore } from "@/lib/canvas-store";

// ── 预设 SubAgent 类型 ──────────────────────────────────────────────
interface PresetDef {
  key: string;
  label: string;
  icon: string;
  system_prompt: string;
  description: string;
  tools: string[];
  max_turns: number;
  timeout_seconds: number;
}

const PRESET_DEFS: PresetDef[] = [
  {
    key: "researcher",
    label: "信息检索专家",
    icon: "🔍",
    description: "Web search, literature lookup, data collection",
    system_prompt: `You are an information retrieval specialist. Complete the delegated research task autonomously and return structured, well-cited results.

<guidelines>
- Use search tools to find the latest and most accurate information
- Cross-verify information from multiple sources
- Organize search results into structured summaries
- Always cite information sources
- If search results are insufficient, clearly state limitations
</guidelines>

<output_format>
When you complete the task, provide:
1. A brief summary of research findings
2. Key data points with citations
3. Information sources referenced
4. Any limitations or gaps in the findings
5. Citations in \`[citation:Title](URL)\` format for external sources
</output_format>

<working_directory>
- User workspace: \`/mnt/user-data/workspace\` is the default working directory
- Prefer workspace-relative paths for scripts and commands
- Save output to the workspace
</working_directory>`,
    tools: ["web_search", "arxiv_search", "web_fetch"],
    max_turns: 60,
    timeout_seconds: 900,
  },
  {
    key: "coder",
    label: "代码执行专家",
    icon: "💻",
    description: "Writing, running, debugging code in sandbox",
    system_prompt: `You are a code execution specialist. Write, execute, and debug code autonomously in the sandbox environment. Return clear results with key outputs.

<guidelines>
- Write high-quality, executable code
- Execute code safely in the sandbox environment
- Handle execution errors and provide fixes
- Output execution results and key logs
- Use workspace-relative paths for files
</guidelines>

<output_format>
For each task:
1. Brief description of the approach
2. The code written (if relevant)
3. Execution results (stdout, key outputs)
4. Any errors or warnings encountered
5. Files created or modified
</output_format>

<working_directory>
- User workspace: \`/mnt/user-data/workspace\` is the default working directory
- Prefer relative paths such as \`script.py\`, \`data/input.csv\`
- Output files should be saved to the workspace
</working_directory>`,
    tools: ["bash", "file_read", "file_write", "list_files", "glob_tool", "grep_tool", "str_replace"],
    max_turns: 60,
    timeout_seconds: 600,
  },
  {
    key: "analyst",
    label: "数据分析专家",
    icon: "📊",
    description: "Data cleaning, statistical analysis, visualization",
    system_prompt: `You are a data analysis specialist. Process data autonomously and return clear analytical results with visualizations when appropriate.

<guidelines>
- Clean and preprocess raw data before analysis
- Perform statistical analysis and hypothesis testing
- Generate data visualizations (charts, plots)
- Extract and clearly state data insights
- Use professional data analysis libraries (pandas, numpy, matplotlib)
</guidelines>

<output_format>
For each task:
1. Summary of data and preprocessing steps
2. Analysis methodology
3. Key findings with statistical measures
4. Visualizations generated (file paths)
5. Conclusions and recommendations
</output_format>

<working_directory>
- User workspace: \`/mnt/user-data/workspace\` is the default working directory
- Prefer relative paths for data files
- Save charts and outputs to the workspace
</working_directory>`,
    tools: ["bash", "file_read", "file_write", "web_search"],
    max_turns: 60,
    timeout_seconds: 900,
  },
  {
    key: "writer",
    label: "文档撰写专家",
    icon: "📝",
    description: "Structured documents, reports, config files",
    system_prompt: `You are a document writing specialist. Produce structured, professional documents and configuration files autonomously.

<guidelines>
- Generate structured documents according to requirements and templates
- Ensure document formatting is consistent and complete
- Generate technical configuration files
- Use professional terminology, maintain document consistency
- Output ready-to-use document content
</guidelines>

<output_format>
For each task:
1. Document type and structure overview
2. The complete generated content
3. File paths where documents are saved
4. Any assumptions or conventions used
</output_format>

<working_directory>
- User workspace: \`/mnt/user-data/workspace\` is the default working directory
- Save generated documents and config files to the workspace
- Use descriptive file names reflecting the content
</working_directory>`,
    tools: ["file_read", "file_write", "str_replace", "list_files"],
    max_turns: 40,
    timeout_seconds: 600,
  },
  {
    key: "reviewer",
    label: "审查专家",
    icon: "🔎",
    description: "Code review, document proofreading, quality inspection",
    system_prompt: `You are a review specialist. Carefully inspect code, documents, or configurations and provide specific, actionable feedback.

<guidelines>
- Carefully check code or documents for errors
- Identify potential issues and risks
- Provide specific, actionable improvement suggestions
- Verify output meets requirements
- Give clear pass/fail conclusions with reasoning
</guidelines>

<output_format>
For each review:
1. Scope of review
2. Issues found (severity: critical / major / minor)
3. Specific improvement suggestions for each issue
4. Overall assessment (pass / pass with changes / fail)
5. Summary of recommendations
</output_format>

<working_directory>
- User workspace: \`/mnt/user-data/workspace\` is the default working directory
- Review files are in the workspace or specified paths
</working_directory>`,
    tools: ["file_read", "list_files", "glob_tool", "grep_tool"],
    max_turns: 30,
    timeout_seconds: 600,
  },
];

// ── 模板标签注入 ──────────────────────────────────────────────────────
const PROMPT_TEMPLATE_TAGS = [
  { tag: "<guidelines>", label: "行为指南" },
  { tag: "<output_format>", label: "输出格式" },
  { tag: "<working_directory>", label: "工作目录" },
  { tag: "<citations>", label: "引用规范" },
];

// ── Props ────────────────────────────────────────────────────────────
interface ConfigPanelProps {
  nodeId: string;
  config: SubAgentConfig;
  isEntryPoint: boolean;
  onClose: () => void;
}

export default function ConfigPanel({ nodeId, config, isEntryPoint, onClose }: ConfigPanelProps) {
  const { updateNodeConfig } = useCanvasStore();
  const [copied, setCopied] = useState(false);

  const update = useCallback(
    <K extends keyof SubAgentConfig>(key: K, value: SubAgentConfig[K]) => {
      updateNodeConfig(nodeId, { [key]: value });
    },
    [nodeId, updateNodeConfig]
  );

  // ── 预设应用 ──
  const applyPreset = useCallback(
    (preset: PresetDef) => {
      update("name", preset.key);
      update("display_name", preset.label);
      update("description", preset.description);
      update("system_prompt", preset.system_prompt);
      update("tools", preset.tools);
      update("max_turns", preset.max_turns);
      update("timeout_seconds", preset.timeout_seconds);
    },
    [update]
  );

  const currentPreset = PRESET_DEFS.find((p) => p.key === config.name);

  // ── 复制提示词 ──
  const copyPrompt = useCallback(async () => {
    await navigator.clipboard.writeText(config.system_prompt);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [config.system_prompt]);

  const tempLabels = ["0 精确", "1 平衡", "2 创意"];

  return (
    <aside className="w-80 border-l bg-white flex-shrink-0 overflow-y-auto">
      {/* ── Header ── */}
      <div className="flex items-center justify-between p-4 border-b sticky top-0 bg-white z-10">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">
            {isEntryPoint ? "🎯 Lead Agent 配置" : "🤖 SubAgent 配置"}
          </h3>
          {isEntryPoint && (
            <p className="text-[11px] text-slate-400 mt-0.5">name 和 role 不可编辑</p>
          )}
        </div>
        <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors">
          <X className="w-4 h-4 text-slate-500" />
        </button>
      </div>

      <div className="p-4 space-y-5">
        {/* ════════════════════════════════════════════════════════════
            Section: 预设类型 (SubAgent only)
            ════════════════════════════════════════════════════════════ */}
        {!isEntryPoint && (
          <div>
            <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3 flex items-center gap-1">
              <Zap className="w-3 h-3" /> 预设类型
            </p>
            <div className="grid grid-cols-2 gap-1.5">
              {PRESET_DEFS.map((preset) => (
                <button
                  key={preset.key}
                  onClick={() => applyPreset(preset)}
                  className={`text-left px-3 py-2 rounded-lg border text-xs transition-all ${
                    currentPreset?.key === preset.key
                      ? "border-slate-900 bg-slate-50 ring-1 ring-slate-200"
                      : "border-slate-200 hover:border-slate-300 hover:bg-slate-50"
                  }`}
                >
                  <span className="text-base">{preset.icon}</span>
                  <p className="font-medium text-slate-800 mt-0.5">{preset.label}</p>
                  <p className="text-[10px] text-slate-400 mt-0.5 leading-tight">{preset.description}</p>
                </button>
              ))}
              <button
                onClick={() => {
                  update("name", "");
                  update("display_name", "自定义 SubAgent");
                }}
                className={`text-left px-3 py-2 rounded-lg border text-xs transition-all ${
                  !currentPreset
                    ? "border-slate-900 bg-slate-50 ring-1 ring-slate-200"
                    : "border-slate-200 hover:border-slate-300 hover:bg-slate-50"
                }`}
              >
                <span className="text-base">🛠️</span>
                <p className="font-medium text-slate-800 mt-0.5">自定义</p>
                <p className="text-[10px] text-slate-400 mt-0.5 leading-tight">完全自定义配置</p>
              </button>
            </div>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════════
            Section: 基本信息
            ════════════════════════════════════════════════════════════ */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">
            基本信息
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                name <span className="text-slate-400 font-normal">(内部标识)</span>
              </label>
              <input
                type="text"
                value={config.name}
                onChange={(e) => update("name", e.target.value)}
                disabled={isEntryPoint}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300 disabled:bg-slate-50 disabled:text-slate-400"
                placeholder="agent_name"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                display_name <span className="text-slate-400 font-normal">(显示名称)</span>
              </label>
              <input
                type="text"
                value={config.display_name}
                onChange={(e) => update("display_name", e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300"
                placeholder="例如: 信息检索专家"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">description</label>
              <textarea
                value={config.description}
                onChange={(e) => update("description", e.target.value)}
                rows={2}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300 resize-none"
                placeholder="描述这个 Agent 的职责..."
              />
            </div>
          </div>
        </div>

        {/* ════════════════════════════════════════════════════════════
            Section: 提示词
            ════════════════════════════════════════════════════════════ */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1">
              <Code className="w-3 h-3" /> 提示词
            </p>
            <div className="flex items-center gap-1">
              <button
                onClick={copyPrompt}
                className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-700 px-2 py-0.5 rounded hover:bg-slate-100 transition-colors"
              >
                {copied ? (
                  <>
                    <Check className="w-3 h-3 text-emerald-500" /> 已复制
                  </>
                ) : (
                  <>
                    <Copy className="w-3 h-3" /> 复制
                  </>
                )}
              </button>
              {currentPreset && (
                <span className="text-[10px] px-1.5 py-0.5 bg-amber-50 text-amber-600 rounded border border-amber-200">
                  预设 · 只读
                </span>
              )}
            </div>
          </div>

          {/* 模板标签注入工具栏 (仅自定义) */}
          {!currentPreset && (
            <div className="flex flex-wrap gap-1 mb-2">
              {PROMPT_TEMPLATE_TAGS.map(({ tag, label }) => (
                <button
                  key={tag}
                  onClick={() => {
                    const ta = document.getElementById("prompt-textarea") as HTMLTextAreaElement | null;
                    if (ta) {
                      const start = ta.selectionStart;
                      const end = ta.selectionEnd;
                      const newText =
                        config.system_prompt.slice(0, start) +
                        `\n${tag}>\n\n</${tag.replace("<", "</")}` +
                        config.system_prompt.slice(end);
                      update("system_prompt", newText);
                    }
                  }}
                  className="text-[10px] px-2 py-0.5 bg-slate-100 hover:bg-slate-200 text-slate-600 rounded border border-slate-200 transition-colors"
                >
                  + {label}
                </button>
              ))}
            </div>
          )}

          {currentPreset ? (
            /* 预设: 只读预览 */
            <pre className="p-3 text-xs text-slate-600 whitespace-pre-wrap max-h-48 overflow-y-auto bg-slate-50 border border-slate-200 rounded-lg font-mono leading-relaxed">
              {config.system_prompt}
            </pre>
          ) : (
            /* 自定义: 可编辑 */
            <textarea
              id="prompt-textarea"
              value={config.system_prompt}
              onChange={(e) => update("system_prompt", e.target.value)}
              rows={10}
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300 resize-none font-mono"
              placeholder="输入 system_prompt..."
            />
          )}
        </div>

        {/* ════════════════════════════════════════════════════════════
            Section: 模型 & 超时
            ════════════════════════════════════════════════════════════ */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3 flex items-center gap-1">
            <Cpu className="w-3 h-3" /> 模型 & 超时
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">model</label>
              <select
                value={config.model}
                onChange={(e) => update("model", e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300 bg-white"
              >
                <option value="inherit">继承父 Agent</option>
                <option value="gpt-4o">gpt-4o</option>
                <option value="gpt-4o-mini">gpt-4o-mini</option>
                <option value="claude-sonnet-4-6">Claude Sonnet 4</option>
                <option value="claude-fable-5">Claude Fable 5</option>
                <option value="qwen3.6-plus">通义千问 3.6 Plus</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                temperature: {config.temperature}
              </label>
              <input
                type="range"
                min="0"
                max="2"
                step="0.1"
                value={config.temperature}
                onChange={(e) => update("temperature", parseFloat(e.target.value))}
                className="w-full accent-slate-700"
              />
              <div className="flex justify-between text-[10px] text-slate-400 mt-0.5">
                {tempLabels.map((l) => (
                  <span key={l}>{l}</span>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1">max_turns</label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={config.max_turns}
                  onChange={(e) => update("max_turns", parseInt(e.target.value) || 10)}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-1 flex items-center gap-1">
                  <Clock className="w-3 h-3 text-slate-400" />
                  timeout (s)
                </label>
                <input
                  type="number"
                  min={30}
                  max={3600}
                  step={30}
                  value={config.timeout_seconds}
                  onChange={(e) => update("timeout_seconds", parseInt(e.target.value) || 900)}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300"
                />
              </div>
            </div>
          </div>
        </div>

        {/* ════════════════════════════════════════════════════════════
            Section: 工具
            ════════════════════════════════════════════════════════════ */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">工具</p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                tools <span className="text-slate-400 font-normal">(逗号分隔, 留空=继承全部)</span>
              </label>
              <input
                type="text"
                value={config.tools?.join(", ") || ""}
                onChange={(e) =>
                  update(
                    "tools",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                placeholder="web_search, bash, file_read"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300"
              />
              {currentPreset && (
                <p className="text-[10px] text-slate-400 mt-1">
                  预设工具: {currentPreset.tools.join(", ")}
                </p>
              )}
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                disallowed_tools <span className="text-slate-400 font-normal">(逗号分隔)</span>
              </label>
              <input
                type="text"
                value={config.disallowed_tools?.join(", ") || ""}
                onChange={(e) =>
                  update(
                    "disallowed_tools",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                placeholder="task, ask_clarification, present_files"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-slate-200 focus:border-slate-300"
              />
            </div>
          </div>
        </div>

        {/* ════════════════════════════════════════════════════════════
            Footer hint
            ════════════════════════════════════════════════════════════ */}
        {currentPreset && config.name && (
          <div className="p-3 bg-blue-50 border border-blue-100 rounded-lg">
            <p className="text-[11px] text-blue-700 leading-relaxed">
              ✅ 已选择「{currentPreset.label}」预设。提示词为只读。点击"自定义"可自由编辑。
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}
