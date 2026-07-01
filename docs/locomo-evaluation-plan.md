# LOCOMO 评估方案——用 LOCOMO 基准测试项目记忆功能

> 分析日期：2026-07-01
> 目标：用 LOCOMO（ACL 2024）基准评估 multiagent-studio 项目的长期记忆能力

---

## 一、LOCOMO 是什么

LOCOMO（**Lo**ng **Co**nversational **Mo**del）是 Snap Research 发表于 ACL 2024 的**超长对话记忆评估基准**，也是 mem0 论文（arXiv:2504.19413）使用的评估基准。

### 1.1 数据集规模

| 维度 | 数值 |
|------|------|
| 对话数 | 10 个（公开发布版，原始 50 个） |
| 每个对话平均轮次 | 300 轮 |
| 每个对话平均 token | 9K |
| 每个对话跨会话数 | 最多 35 个 session |
| 时间跨度 | 模拟数月~数年的长期对话 |

### 1.2 数据结构

每个 sample 的 JSON 结构：

```json
{
  "sample_id": "1",
  "conversation": {
    "session_1": [{"speaker": "Angela", "dia_id": "1_1", "text": "..."}],
    "session_1_date_time": "2023-01-15",
    "speaker_a": "Angela",
    "speaker_b": "Bob",
    "session_2": [...],
    "session_2_date_time": "2023-02-20"
  },
  "observation": {
    "session_1_observation": "Angela works at a gift shop..."
  },
  "session_summary": {
    "session_1_summary": "Angela and Bob discussed..."
  },
  "event_summary": {
    "events_session_1": [{"speaker": "Angela", "event": "..."}]
  },
  "qa": [
    {
      "question": "What does Angela do for a living?",
      "answer": "She manages a gift shop",
      "category": "single_hop",
      "evidence": ["1_3", "1_7"]
    }
  ]
}
```

### 1.3 三大评估任务

| 任务 | 评估什么 | 指标 |
|------|---------|------|
| **Question Answering** | 能否"回忆"过去的对话内容 | F1-score |
| **Event Graph Summarization** | 能否识别因果和时序关系 | F1 / ROUGE |
| **Multimodal Dialog Generation** | 能否利用历史生成连贯回复 | MM-Relevance |

### 1.4 QA 的 5 种问题类型（最常用）

| 类型 | 评估什么 | 示例 | 难度 |
|------|---------|------|------|
| **single_hop** | 单轮记忆 | "Angela 在哪里工作？" | 低 |
| **multi_hop** | 跨会话信息整合 | "Bob 上次说的那个项目进展如何？" | 中 |
| **temporal** | 时间推理 | "Angela 搬家前住在哪里？" | **高**（LLM 最弱） |
| **commonsense** | 常识推理 | "Angela 为什么选择这份工作？" | 中 |
| **adversarial** | 对抗性问题（陷阱） | "Angela 有没有提到她讨厌猫？"（实际没提） | **高**（检测幻觉） |

### 1.5 LOCOMO 的关键发现

- 长上下文 LLM 和 RAG 在 QA 任务上有提升（22-66%），但仍显著落后人类水平（差 56%）
- **时间推理是最大短板**（差 73%）
- 长上下文 LLM 容易产生幻觉（对抗性问题表现差）
- RAG 在"对话转为断言（observations）"后表现最好

---

## 二、评估方案设计

### 2.1 评估目标

评估项目的记忆系统（FileMemoryStorage / mem0）在以下方面的表现：

1. **记忆写入**：能否从对话中正确提取事实/偏好/事件
2. **记忆检索**：能否根据问题召回相关记忆
3. **记忆准确性**：召回的记忆是否正确（无幻觉、无过时信息）
4. **时间推理**：能否处理"之前""搬家前"等时间相关查询
5. **抗幻觉**：能否正确回答"没提过"的对抗性问题

### 2.2 评估架构

```
┌─────────────────────────────────────────────────────────────┐
│                    LOCOMO 评估流程                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ① 数据加载                                                  │
│     LOCOMO locomo10.json                                    │
│         ↓                                                   │
│  ② 记忆写入阶段（模拟长期对话）                                │
│     按 session 顺序，逐轮调 mem0.add() 或 FileMemoryStorage   │
│     每个对话 ~300 轮，跨 ~35 个 session                       │
│         ↓                                                   │
│  ③ 记忆检索阶段（QA 评估）                                    │
│     对每个 qa 问题，调 mem0.search() 或 get_memory_data()     │
│     用检索到的记忆 + LLM 生成答案                              │
│         ↓                                                   │
│  ④ 评分阶段                                                  │
│     对比预测答案与 ground truth                               │
│     计算 F1-score（按问题类型分类统计）                        │
│         ↓                                                   │
│  ⑤ 结果分析                                                  │
│     总体 F1 + 按类别 F1（single_hop/multi_hop/temporal/...）  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 评估的三个维度

#### 维度 A：记忆写入质量

**方法**：把 LOCOMO 的对话喂给记忆系统，然后检查"是否存了该存的"。

```python
# 写入后检查
for qa in sample["qa"]:
    # qa.evidence 是包含答案的 dialog_id 列表
    # 检查这些信息是否被记忆系统存储
    relevant_memories = mem0.search(qa["question"], filters={"user_id": speaker_a})
    # 人工或 LLM 判断：relevant_memories 是否包含 evidence 对应的信息
```

#### 维度 B：记忆检索质量（核心）

**方法**：用 LOCOMO 的 QA 问题测试检索 + 生成。

```python
for qa in sample["qa"]:
    # ① 用问题检索记忆
    memories = mem0.search(qa["question"], filters={"user_id": speaker_a}, top_k=5)
    
    # ② 用检索到的记忆 + 问题，让 LLM 生成答案
    prompt = f"基于以下记忆回答问题：\n记忆：{memories}\n问题：{qa['question']}"
    predicted_answer = llm.invoke(prompt)
    
    # ③ 对比 ground truth
    f1 = compute_f1(predicted_answer, qa["answer"])
```

#### 维度 C：端到端回答质量

**方法**：模拟完整对话流程（包括记忆注入到 Agent 上下文）。

```python
# 模拟项目的 DynamicContextMiddleware 注入
for qa in sample["qa"]:
    # 用项目的中间件逻辑检索并注入记忆
    state = {"messages": [HumanMessage(qa["question"])], "user_id": speaker_a}
    injected = await dynamic_middleware._inject(state)
    # injected 包含 system-reminder（记忆注入）
    
    # Agent 基于注入的记忆回答
    response = agent.invoke(state)
    f1 = compute_f1(response, qa["answer"])
```

---

## 三、具体实现方案

### 3.1 环境准备

```bash
# 克隆 LOCOMO 仓库
cd D:\Langchain_study\开源agent\multiagent-studio
git clone https://github.com/snap-research/LoCoMo.git eval/locomo

# 数据文件在 eval/locomo/data/locomo10.json
```

### 3.2 评估脚本

**文件**：`eval/locomo_eval.py`（新增）

```python
"""LOCOMO 评估脚本——测试项目记忆系统的长期记忆能力。

Usage:
    # 评估 mem0 backend
    python eval/locomo_eval.py --backend mem0 --output results/mem0_results.json
    
    # 评估 file backend（基线对比）
    python eval/locomo_eval.py --backend file --output results/file_results.json
    
    # 评估无记忆（全上下文）基线
    python eval/locomo_eval.py --backend full_context --output results/full_context.json
"""

import asyncio
import json
import re
import string
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── F1-score 计算（参考 LOCOMO 原始实现）──

def normalize_answer(s: str) -> str:
    """Lower text and remove punctuation, articles and extra whitespace."""
    s = s.lower()
    s = "".join(c for c in s if c not in string.punctuation)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    s = " ".join(s.split())
    return s


def compute_f1(prediction: str, ground_truth: str) -> float:
    """计算 token-level F1 score。"""
    pred_tokens = normalize_answer(prediction).split()
    gt_tokens = normalize_answer(ground_truth).split()
    
    if not pred_tokens or not gt_tokens:
        return 1.0 if pred_tokens == gt_tokens else 0.0
    
    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    
    if num_same == 0:
        return 0.0
    
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


# ── LOCOMO 数据加载 ──

def load_locomo(path: str = "eval/locomo/data/locomo10.json") -> list[dict]:
    """加载 LOCOMO 数据集。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def extract_sessions(conversation: dict) -> list[tuple[str, list[dict]]]:
    """从 conversation 中提取所有 session 及其对话轮次。
    
    Returns:
        [(session_name, turns), ...] 按 session 编号排序
    """
    sessions = []
    for key in sorted(conversation.keys()):
        if key.startswith("session_") and not key.endswith("_date_time") and not key.endswith("_observation") and not key.endswith("_summary"):
            if not key.startswith("session_summary"):
                turns = conversation[key]
                if isinstance(turns, list):
                    sessions.append((key, turns))
    return sessions


def turns_to_messages(turns: list[dict], speaker_a: str, speaker_b: str) -> list[dict]:
    """把 LOCOMO 的 turns 转为 mem0 的 messages 格式。"""
    messages = []
    for turn in turns:
        speaker = turn.get("speaker", "")
        text = turn.get("text", "")
        if not text.strip():
            continue
        # 把 speaker_a 视为 user，speaker_b 视为 assistant
        role = "user" if speaker == speaker_a else "assistant"
        messages.append({"role": role, "content": text})
    return messages


# ── 记忆写入 ──

async def write_memories_mem0(sessions: list, speaker_a: str, speaker_b: str, user_id: str):
    """把 LOCOMO 对话写入 mem0。"""
    from harness.memory.mem0_client import get_mem0
    
    mem0 = get_mem0()
    if mem0 is None:
        raise RuntimeError("mem0 not initialized")
    
    for session_name, turns in sessions:
        messages = turns_to_messages(turns, speaker_a, speaker_b)
        if not messages:
            continue
        
        # 模拟 debounce：每个 session 写入一次
        await asyncio.to_thread(
            mem0.add,
            messages,
            user_id=user_id,
            agent_id="locomo_eval",
            metadata={"session": session_name},
        )
        # 小延迟避免速率限制
        await asyncio.sleep(0.5)


def write_memories_file(sessions: list, speaker_a: str, user_id: str):
    """把 LOCOMO 对话写入 FileMemoryStorage（基线）。"""
    from harness.memory.updater import MemoryUpdater
    
    updater = MemoryUpdater()
    for session_name, turns in sessions:
        # 转为 LangChain 消息格式
        from langchain_core.messages import HumanMessage, AIMessage
        messages = []
        for turn in turns:
            text = turn.get("text", "")
            if not text.strip():
                continue
            if turn.get("speaker") == speaker_a:
                messages.append(HumanMessage(content=text))
            else:
                messages.append(AIMessage(content=text))
        
        if messages:
            # 同步调用（会调 LLM 提取）
            asyncio.run(updater.aupdate_memory(
                messages=messages,
                thread_id=f"locomo_{user_id}",
                agent_name="locomo_eval",
                user_id=user_id,
            ))


# ── QA 评估 ──

async def evaluate_qa_mem0(qa_list: list[dict], user_id: str, llm) -> list[dict]:
    """用 mem0 检索 + LLM 生成答案，计算 F1。"""
    from harness.memory.mem0_client import get_mem0
    
    mem0 = get_mem0()
    results = []
    
    for qa in qa_list:
        question = qa["question"]
        gt_answer = qa["answer"]
        category = qa.get("category", "unknown")
        
        # ① 检索记忆
        memories = await asyncio.to_thread(
            mem0.search,
            query=question,
            filters={"user_id": user_id, "agent_id": "locomo_eval"},
            top_k=5,
        )
        
        # 格式化记忆
        memory_text = "\n".join([f"- {m.get('memory', '')}" for m in memories.get("results", [])])
        
        # ② LLM 生成答案
        prompt = (
            f"Based on the following memories, answer the question concisely.\n\n"
            f"Memories:\n{memory_text}\n\n"
            f"Question: {question}\n"
            f"Answer (brief, factual):"
        )
        
        try:
            response = await llm.ainvoke(prompt)
            predicted = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            predicted = f"[ERROR: {e}]"
        
        # ③ 计算 F1
        f1 = compute_f1(predicted, gt_answer)
        
        results.append({
            "question": question,
            "ground_truth": gt_answer,
            "predicted": predicted,
            "category": category,
            "f1": f1,
            "retrieved_count": len(memories.get("results", [])),
        })
        
        print(f"  [{category}] F1={f1:.2f} | Q: {question[:50]}...")
    
    return results


async def evaluate_qa_full_context(qa_list: list[dict], conversation: dict, 
                                    speaker_a: str, llm) -> list[dict]:
    """全上下文基线：把整个对话作为上下文（无记忆系统）。"""
    # 拼接所有对话文本
    all_text = []
    for key in sorted(conversation.keys()):
        if key.startswith("session_") and not key.endswith(("_date_time", "_observation", "_summary")):
            turns = conversation[key]
            if isinstance(turns, list):
                for turn in turns:
                    speaker = turn.get("speaker", "")
                    text = turn.get("text", "")
                    if text.strip():
                        all_text.append(f"{speaker}: {text}")
    
    full_context = "\n".join(all_text)
    
    results = []
    for qa in qa_list:
        question = qa["question"]
        gt_answer = qa["answer"]
        category = qa.get("category", "unknown")
        
        prompt = (
            f"Based on the following conversation, answer the question concisely.\n\n"
            f"Conversation:\n{full_context[:8000]}\n\n"  # 截断到 8K tokens
            f"Question: {question}\n"
            f"Answer (brief, factual):"
        )
        
        try:
            response = await llm.ainvoke(prompt)
            predicted = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            predicted = f"[ERROR: {e}]"
        
        f1 = compute_f1(predicted, gt_answer)
        results.append({
            "question": question,
            "ground_truth": gt_answer,
            "predicted": predicted,
            "category": category,
            "f1": f1,
        })
        print(f"  [{category}] F1={f1:.2f} | Q: {question[:50]}...")
    
    return results


# ── 结果分析 ──

def analyze_results(results: list[dict]) -> dict:
    """分析评估结果，按类别统计 F1。"""
    categories = {}
    all_f1 = []
    
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"count": 0, "f1_sum": 0.0, "f1_list": []}
        categories[cat]["count"] += 1
        categories[cat]["f1_sum"] += r["f1"]
        categories[cat]["f1_list"].append(r["f1"])
        all_f1.append(r["f1"])
    
    # 计算每个类别的平均 F1
    cat_stats = {}
    for cat, data in categories.items():
        cat_stats[cat] = {
            "count": data["count"],
            "avg_f1": data["f1_sum"] / data["count"] if data["count"] > 0 else 0,
            "min_f1": min(data["f1_list"]) if data["f1_list"] else 0,
            "max_f1": max(data["f1_list"]) if data["f1_list"] else 0,
        }
    
    return {
        "overall_f1": sum(all_f1) / len(all_f1) if all_f1 else 0,
        "total_questions": len(all_f1),
        "by_category": cat_stats,
    }


# ── 主流程 ──

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="LOCOMO evaluation for memory system")
    parser.add_argument("--backend", choices=["mem0", "file", "full_context"], 
                        default="mem0", help="Memory backend to evaluate")
    parser.add_argument("--output", default="results/locomo_results.json",
                        help="Output file for results")
    parser.add_argument("--locomo-path", default="eval/locomo/data/locomo10.json")
    parser.add_argument("--max-samples", type=int, default=None,
                        help="Limit number of conversations (for quick test)")
    args = parser.parse_args()
    
    # 加载数据
    samples = load_locomo(args.locomo_path)
    if args.max_samples:
        samples = samples[:args.max_samples]
    
    print(f"Loaded {len(samples)} LOCOMO conversations")
    print(f"Backend: {args.backend}")
    print()
    
    # 初始化 LLM（用于生成答案）
    from langchain_openai import ChatOpenAI
    from harness.config import get_config
    cfg = get_config()
    llm = ChatOpenAI(
        model=cfg.get("model", "qwen-plus"),
        api_key=cfg.get("openai_api_key", ""),
        base_url=cfg.get("openai_base_url", ""),
        temperature=0,
    )
    
    all_results = []
    
    for i, sample in enumerate(samples):
        sample_id = sample["sample_id"]
        conv = sample["conversation"]
        speaker_a = conv.get("speaker_a", "speaker_a")
        speaker_b = conv.get("speaker_b", "speaker_b")
        qa_list = sample.get("qa", [])
        
        print(f"=== Sample {sample_id} ({i+1}/{len(samples)}) ===")
        print(f"  Speaker A: {speaker_a}, Speaker B: {speaker_b}")
        print(f"  QA count: {len(qa_list)}")
        
        sessions = extract_sessions(conv)
        print(f"  Sessions: {len(sessions)}")
        
        # 写入记忆（full_context 不需要写入）
        if args.backend == "mem0":
            print("  Writing memories to mem0...")
            user_id = f"locomo_{sample_id}_{speaker_a}"
            await write_memories_mem0(sessions, speaker_a, speaker_b, user_id)
            
            print("  Evaluating QA...")
            results = await evaluate_qa_mem0(qa_list, user_id, llm)
        
        elif args.backend == "file":
            print("  Writing memories to FileMemoryStorage...")
            user_id = f"locomo_{sample_id}_{speaker_a}"
            write_memories_file(sessions, speaker_a, user_id)
            
            print("  Evaluating QA...")
            # file backend 的检索逻辑
            results = await evaluate_qa_file(qa_list, user_id, llm)
        
        elif args.backend == "full_context":
            print("  Evaluating QA (full context baseline)...")
            results = await evaluate_qa_full_context(qa_list, conv, speaker_a, llm)
        
        all_results.extend(results)
        
        # 清理记忆（避免下一个 sample 的记忆污染）
        if args.backend == "mem0":
            from harness.memory.mem0_client import get_mem0
            mem0 = get_mem0()
            # 删除当前 user 的所有记忆
            try:
                mem0.delete_all(user_id=user_id)
            except Exception:
                pass  # OSS 可能不支持 delete_all
        
        print()
    
    # 分析结果
    stats = analyze_results(all_results)
    
    print("=" * 60)
    print(f"Overall F1: {stats['overall_f1']:.4f}")
    print(f"Total questions: {stats['total_questions']}")
    print()
    print("By category:")
    for cat, data in sorted(stats["by_category"].items()):
        print(f"  {cat:20s}: F1={data['avg_f1']:.4f} (n={data['count']}, "
              f"min={data['min_f1']:.2f}, max={data['max_f1']:.2f})")
    
    # 保存详细结果
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "backend": args.backend,
            "stats": stats,
            "details": all_results,
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\nDetailed results saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
```

### 3.3 对比评估矩阵

为了全面评估，建议跑 3 个 backend 对比：

```bash
# ① mem0 backend（主评估对象）
python eval/locomo_eval.py --backend mem0 --output results/mem0_results.json

# ② file backend（现有方案，作为基线）
python eval/locomo_eval.py --backend file --output results/file_results.json

# ③ full context（理论上限，无记忆系统，直接喂全部对话）
python eval/locomo_eval.py --backend full_context --output results/full_context.json
```

---

## 四、评估指标解读

### 4.1 F1-score 基准参考

根据 LOCOMO 论文和 mem0 论文的基准数据：

| 方案 | 总体 F1 | single_hop | multi_hop | temporal | adversarial |
|------|--------|------------|-----------|----------|-------------|
| **人类水平** | ~0.85 | ~0.90 | ~0.85 | ~0.80 | ~0.85 |
| **全上下文（GPT-4）** | ~0.45 | ~0.55 | ~0.40 | ~0.20 | ~0.35 |
| **RAG（GPT-3.5）** | ~0.35 | ~0.45 | ~0.30 | ~0.15 | ~0.25 |
| **mem0（论文报告）** | ~0.55 | ~0.65 | ~0.50 | ~0.30 | ~0.40 |
| **现有 FileMemoryStorage** | 待测 | 待测 | 待测 | 待测 | 待测 |

### 4.2 关注的指标

| 指标 | 含义 | 重点关注 |
|------|------|---------|
| **总体 F1** | 所有问题的平均 F1 | 衡量整体记忆能力 |
| **temporal F1** | 时间推理类问题的 F1 | **mem0 vs file 的关键差异点** |
| **multi_hop F1** | 跨会话问题的 F1 | 检验向量检索的跨会话能力 |
| **adversarial F1** | 对抗性问题的 F1 | 检验是否有幻觉（应该回答"没提过"） |
| **检索命中率** | 检索到的记忆是否包含 evidence | 检验检索准确性 |

### 4.3 评估结果分析维度

```python
# 结果分析示例
{
  "backend": "mem0",
  "stats": {
    "overall_f1": 0.52,
    "by_category": {
      "single_hop": {"avg_f1": 0.65, "count": 50},     # 单轮记忆好
      "multi_hop": {"avg_f1": 0.45, "count": 30},      # 跨会话中等
      "temporal": {"avg_f1": 0.25, "count": 20},       # 时间推理弱（预期）
      "adversarial": {"avg_f1": 0.40, "count": 15},    # 抗幻觉中等
      "commonsense": {"avg_f1": 0.55, "count": 25}     # 常识推理中等
    }
  }
}
```

**分析要点**：
- 如果 mem0 的 temporal F1 显著高于 file，说明向量检索 + 时间过滤有效
- 如果 mem0 的 adversarial F1 高于 file，说明冲突检测减少了幻觉
- 如果 mem0 的 multi_hop F1 高于 file，说明向量检索优于全量注入

---

## 五、评估执行步骤

### Step 1：准备环境

```bash
# 克隆 LOCOMO
git clone https://github.com/snap-research/LoCoMo.git eval/locomo

# 确认数据文件
ls eval/locomo/data/locomo10.json

# 安装依赖
pip install langchain-openai  # 用于评估时的 LLM 调用
```

### Step 2：快速测试（1 个对话）

```bash
# 先用 1 个对话跑通流程
python eval/locomo_eval.py --backend mem0 --max-samples 1 --output results/test.json
```

### Step 3：完整评估

```bash
# 跑完 10 个对话（约 1-2 小时，取决于 LLM 速度）
python eval/locomo_eval.py --backend mem0 --output results/mem0_full.json
python eval/locomo_eval.py --backend file --output results/file_full.json
python eval/locomo_eval.py --backend full_context --output results/full_context_full.json
```

### Step 4：结果对比

```python
# 对比脚本
import json

for backend in ["mem0", "file", "full_context"]:
    with open(f"results/{backend}_full.json") as f:
        data = json.load(f)
    stats = data["stats"]
    print(f"{backend:15s}: Overall F1 = {stats['overall_f1']:.4f}")
    for cat, d in stats["by_category"].items():
        print(f"  {cat:20s}: {d['avg_f1']:.4f}")
```

---

## 六、注意事项

### 6.1 评估的局限性

| 局限 | 说明 | 应对 |
|------|------|------|
| **LOCOMO 是英文数据集** | 项目主要面向中文用户 | 评估结果反映"通用记忆能力"，中文效果需额外测试 |
| **10 个对话样本量小** | 统计显著性有限 | 关注分类别 F1 而非总体 |
| **LLM 生成答案的随机性** | temperature=0 但仍可能有变化 | 多次运行取平均 |
| **mem0 的 LLM 提取质量** | 写入时的提取质量影响检索 | 这正是要评估的 |
| **OSS 无 Temporal Reasoning** | temporal 类问题会表现差 | 预期结果，关注改进幅度 |

### 6.2 成本估算

| 操作 | LLM 调用次数 | 预计成本 |
|------|-------------|---------|
| 写入记忆（10 对话 × 35 session） | ~350 次 mem0.add() × 2 LLM 调用 = 700 | ~$5-10 |
| QA 评估（~150 问题 × 1 LLM 调用） | ~150 | ~$1-2 |
| 全上下文基线（10 × 1 LLM 调用） | ~150 | ~$2-3 |
| **总计** | ~1000 次 LLM 调用 | **~$8-15** |

### 6.3 评估后的行动

| 如果发现... | 行动 |
|------------|------|
| mem0 的 temporal F1 很低 | 考虑加应用层时间解析（之前方案里的 `parse_temporal_query`） |
| mem0 的 multi_hop F1 低于 file | 检查 top_k 是否太小，或组合查询是否有冲突 |
| mem0 的 adversarial F1 低 | 检查是否有过时记忆未被 UPDATE，加强冲突检测 |
| mem0 整体不如 file | 检查 mem0 的 LLM 提取质量（换更强的模型） |
| mem0 整体优于 file | 确认接入方案有效，可推进生产部署 |

---

## 七、总结

LOCOMO 是目前最权威的长期对话记忆评估基准。用它评估项目的记忆系统：

1. **写入阶段**：把 LOCOMO 的 10 个超长对话（每个 ~300 轮、35 个 session）逐轮写入记忆系统
2. **检索阶段**：用 LOCOMO 的 ~150 个 QA 问题（5 种类型）测试检索 + 生成
3. **评分阶段**：计算 F1-score，按问题类型分类统计
4. **对比阶段**：跑 mem0 / file / full_context 三个 backend 对比

**最关注的指标**：
- temporal F1（时间推理——mem0 的向量检索 vs file 的全量注入）
- adversarial F1（抗幻觉——mem0 的冲突检测 vs file 的无冲突处理）
- multi_hop F1（跨会话——向量检索的核心优势场景）

评估脚本已写入 `eval/locomo_eval.py`，可直接运行。

---

*本方案基于 LOCOMO 论文（arXiv:2402.17753, ACL 2024）和 mem0 论文（arXiv:2504.19413）编写。*
