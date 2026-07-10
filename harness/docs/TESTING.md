# Agent Team 功能测试指南

> 当前状态：后端 Team 引擎已完成（`harness/team/`），前端尚未适配 Team 模式。
> 本文档覆盖从 API 层到引擎层的全部测试方法。

---

## 一、测试层次总览

```
┌─────────────────────────────────────────────┐
│ Layer 1: 单元测试 (pytest)                   │
│ TeamTaskStore / TeamMessageBus / Models     │
│ 已完成 13 个测试，可直接运行                  │
├─────────────────────────────────────────────┤
│ Layer 2: API 集成测试 (curl / httpx)         │
│ 通过 HTTP 调用 Harness API 验证完整链路      │
│ 不需要前端                                   │
├─────────────────────────────────────────────┤
│ Layer 3: 端到端场景测试 (Python 脚本)        │
│ 创建 Project → 添加 Member → 发送消息        │
│ → 验证任务创建/分配/执行/完成                │
├─────────────────────────────────────────────┤
│ Layer 4: 前端 + 后端联调 (可选)              │
│ 利用现有前端页面 + 浏览器 DevTools            │
└─────────────────────────────────────────────┘
```

---

## 二、Layer 1：单元测试

### 2.1 运行现有测试

```bash
cd /mnt/d/Langchain_study/开源agent/multiagent-studio

# 运行全部 Team 测试
python -m pytest harness/team/tests/ -v

# 只运行 TaskStore 测试
python -m pytest harness/team/tests/test_task_store.py -v

# 只运行 MessageBus 测试
python -m pytest harness/team/tests/test_message_bus.py -v
```

### 2.2 现有测试覆盖

| 测试 | 验证点 |
|---|---|
| `test_create_task` | 任务创建、自动生成 ID、默认状态 PENDING |
| `test_update_task` | 任务字段更新、状态变更 |
| `test_update_nonexistent` | 更新不存在的任务返回 None |
| `test_list_tasks` | 列出所有任务、按状态过滤 |
| `test_dependency_resolution` | 依赖未完成时阻塞、依赖完成后就绪 |
| `test_circular_dependency_detection` | DFS 三色标记算法检测环 |
| `test_delete_task` | 删除存在/不存在的任务 |
| `test_send_and_receive` | 消息收发 |
| `test_broadcast` | 广播过滤（不发给发送者） |
| `test_unread_tracking` | 未读追踪、已读标记 |
| `test_message_persistence` | JSONL 持久化 + 重新加载 |
| `test_message_loop_detection` | 消息循环检测 (A→B→A→B) |
| `test_no_loop_when_normal` | 正常消息不误判为循环 |

### 2.3 还需要补充的单元测试

```python
# harness/team/tests/test_orchestrator.py
def test_orchestrator_init_with_empty_project():
    """项目无成员时初始化不抛异常."""

def test_select_idle_agent_load_balancing():
    """负载均衡：优先分配给已完成任务少的 member."""

def test_dispatch_loop_stops_on_cancel():
    """取消标志使调度循环退出."""

def test_retry_exhausted_marks_failed():
    """重试 3 次后标记 FAILED + 发送广播."""

# harness/team/tests/test_tools.py
def test_delegate_to_member_rejects_wrong_assignee():
    """委派给非指定的 agent 返回错误."""

def test_task_create_with_dependencies():
    """创建带依赖的任务."""

def test_review_task_approve():
    """审阅通过：REVIEWING → COMPLETED."""

def test_review_task_reject():
    """审阅打回：REVIEWING → PENDING."""
```

---

## 三、Layer 2：API 集成测试 (curl / HTTP)

### 3.1 前置条件

确保 Harness 服务和 App 服务都在运行：

```bash
# 检查 Harness 服务 (端口 8001)
curl -s http://localhost:8001/api/v1/agents/presets | head -c 200

# 检查 App 服务 (端口 8000)
curl -s http://localhost:8000/api/v1/agents?user_id=default | head -c 200
```

如果服务未启动：

```bash
# 启动 Harness
cd harness && python -m uvicorn harness.api.server:app --host 0.0.0.0 --port 8001 &

# 启动 App
cd app && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &
```

### 3.2 测试 Agent 创建（验证 AgentConfig 增强字段）

```bash
# 创建一个带 Team 配置的 Agent
curl -s -X POST http://localhost:8001/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test_coder",
    "display_name": "测试开发者",
    "description": "负责代码编写和调试",
    "soul": "# 角色\n你是后端开发专家。\n\n## 职责\n- 编写高质量代码\n- 修复 bug\n- 代码审查",
    "model": "inherit",
    "tool_groups": ["files", "code", "shell"],
    "memory_scope": "team",
    "can_be_lead": false,
    "can_delegate": false,
    "max_turns": 20,
    "timeout_seconds": 600,
    "isolation": "none",
    "user_id": "default"
  }' | python -m json.tool
```

**预期结果**：返回 `{"status": "created", "name": "test_coder"}`

```bash
# 创建第二个 Agent — 可以作为 Lead
curl -s -X POST http://localhost:8001/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test_lead",
    "display_name": "测试项目经理",
    "description": "负责项目管理和任务分配",
    "soul": "# 角色\n你是项目经理。\n\n## 职责\n- 分析需求\n- 拆分任务\n- 分配给团队成员",
    "model": "inherit",
    "tool_groups": ["files", "search"],
    "memory_scope": "project",
    "can_be_lead": true,
    "can_delegate": true,
    "max_turns": 30,
    "timeout_seconds": 900,
    "isolation": "none",
    "user_id": "default"
  }' | python -m json.tool
```

### 3.3 验证 Agent 配置持久化

```bash
# 获取 Agent 配置
curl -s http://localhost:8001/api/v1/agents/test_coder?user_id=default | python -m json.tool

# 检查字段
curl -s http://localhost:8001/api/v1/agents/test_coder?user_id=default | python -c "
import json, sys
data = json.load(sys.stdin)
agent = data['agent']
assert agent['memory_scope'] == 'team', 'memory_scope mismatch'
assert agent['can_be_lead'] == False, 'can_be_lead mismatch'
assert agent['max_turns'] == 20, 'max_turns mismatch'
print('All field assertions passed!')
for k, v in agent.items():
    print(f'  {k}: {v}')
"
```

### 3.4 测试项目创建和成员管理

```bash
# 创建项目
curl -s -X POST http://localhost:8001/api/v1/projects \
  -H "Content-Type: application/json" \
  -d '{
    "name": "测试项目",
    "description": "用于测试 Agent Team 功能的项目",
    "members": [],
    "user_id": "default"
  }' | python -m json.tool
# 记录返回的 id，例如 "a1b2c3d4"

# 添加成员
PROJECT_ID="a1b2c3d4"  # 替换为上一步返回的 id

curl -s -X POST http://localhost:8001/api/v1/projects/$PROJECT_ID/members \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "test_coder", "user_id": "default"}' | python -m json.tool

curl -s -X POST http://localhost:8001/api/v1/projects/$PROJECT_ID/members \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "test_lead", "user_id": "default"}' | python -m json.tool

# 验证成员已添加
curl -s http://localhost:8001/api/v1/projects/$PROJECT_ID?user_id=default | python -c "
import json, sys
p = json.load(sys.stdin)
assert 'test_coder' in p['members'], 'test_coder not in members'
assert 'test_lead' in p['members'], 'test_lead not in members'
print('Members:', p['members'])
"
```

### 3.5 测试 Thread 创建（含新字段）

```bash
# 通过 Harness API 验证 ExecuteRequest 扩展字段
# 直接调用 App 层的 /api/threads 端点

# 创建绑定项目的 Thread
curl -s -X POST http://localhost:8000/api/threads \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <YOUR_JWT_TOKEN>" \
  -d '{
    "title": "Team 测试会话",
    "project_id": "'$PROJECT_ID'",
    "mode": "team"
  }' | python -m json.tool
```

> **注意**：App 层需要 JWT 认证。如果尚未注册用户，需要先调用 `/api/auth/register` 注册。

### 3.6 测试 Team 模式执行 (关键测试)

```bash
# 直接调用 Harness API（不需要 JWT）
# 模拟 Team 模式执行请求
curl -s -X POST http://localhost:8001/api/v1/execute \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "test-thread-001",
    "user_id": "default",
    "message": "请研究 Python asyncio 的最佳实践并写一份总结报告",
    "project_id": "'$PROJECT_ID'",
    "mode": "team"
  }' 2>&1 | head -100
```

**预期 SSE 事件流**：

```
data: {"type":"team_start","thread_id":"test-thread-001","project_id":"...","members":["test_coder","test_lead"],"mode":"team"}

data: {"type":"team_status","phase":"planning","content":"Lead Agent 正在分析目标并拆解任务..."}

data: {"type":"team_task_update","task":{"id":"...","title":"用户目标: 请研究 Python asyncio...","status":"pending",...}}

data: {"type":"team_status","phase":"dispatching","content":"开始调度任务执行..."}

# ... 如果任务被分配和执行 ...

data: {"type":"team_end","status":"completed","total_rounds":1}
```

---

## 四、Layer 3：端到端场景测试 (Python 脚本)

### 4.1 完整 E2E 测试脚本

创建文件 `harness/team/tests/test_e2e.py`：

```python
"""Agent Team 端到端测试 — 创建 Project → 添加 Member → Team 执行.

运行方式:
    python harness/team/tests/test_e2e.py

前置条件: Harness 服务在 localhost:8001 运行
"""

import asyncio
import json
import httpx
import uuid


HARNESS_URL = "http://localhost:8001"
USER_ID = "default"


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        project_id = None
        
        # ── Step 1: 创建 Agent ──
        print("=" * 60)
        print("Step 1: 创建测试 Agent")
        
        for name, display_name, can_be_lead in [
            ("e2e_lead", "E2E 项目经理", True),
            ("e2e_writer", "E2E 文档写手", False),
        ]:
            resp = await client.post(
                f"{HARNESS_URL}/api/v1/agents",
                json={
                    "name": name,
                    "display_name": display_name,
                    "description": f"E2E 测试 Agent: {display_name}",
                    "soul": f"你是 {display_name}。请完成分配给你的任务并返回结果。",
                    "model": "inherit",
                    "tool_groups": ["files"],
                    "memory_scope": "team",
                    "can_be_lead": can_be_lead,
                    "can_delegate": can_be_lead,
                    "max_turns": 10,
                    "timeout_seconds": 300,
                    "isolation": "none",
                    "user_id": USER_ID,
                },
            )
            result = resp.json()
            assert result.get("status") == "created", f"Failed to create agent {name}: {result}"
            print(f"  ✓ Agent '{name}' created")

        # ── Step 2: 创建 Project ──
        print("\nStep 2: 创建 Project")
        resp = await client.post(
            f"{HARNESS_URL}/api/v1/projects",
            json={
                "name": "E2E 测试项目",
                "description": "端到端测试用的项目",
                "members": [],
                "user_id": USER_ID,
            },
        )
        project = resp.json()
        project_id = project["id"]
        print(f"  ✓ Project '{project_id}' created: {project['name']}")

        # ── Step 3: 添加成员 ──
        print("\nStep 3: 添加成员")
        for agent_name in ["e2e_lead", "e2e_writer"]:
            resp = await client.post(
                f"{HARNESS_URL}/api/v1/projects/{project_id}/members",
                json={"agent_name": agent_name, "user_id": USER_ID},
            )
            updated = resp.json()
            assert agent_name in updated.get("members", []), f"Member {agent_name} not added"
            print(f"  ✓ Member '{agent_name}' added to project")

        # ── Step 4: 验证项目配置 ──
        print("\nStep 4: 验证项目配置")
        resp = await client.get(
            f"{HARNESS_URL}/api/v1/projects/{project_id}",
            params={"user_id": USER_ID},
        )
        project = resp.json()
        assert len(project["members"]) == 2
        print(f"  ✓ Project has {len(project['members'])} members: {project['members']}")

        # ── Step 5: Team 模式执行 ──
        print("\nStep 5: Team 模式执行 (SSE stream)")
        thread_id = f"e2e-thread-{uuid.uuid4().hex[:8]}"
        events_received = []
        
        async with client.stream(
            "POST",
            f"{HARNESS_URL}/api/v1/execute",
            json={
                "thread_id": thread_id,
                "user_id": USER_ID,
                "message": "请用 markdown 写一份简短的团队协作指南（100 字以内）",
                "project_id": project_id,
                "mode": "team",
            },
        ) as response:
            assert response.status_code == 200, f"Execute failed: {response.status_code}"
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        event = json.loads(data_str)
                        events_received.append(event)
                        event_type = event.get("type", "?")
                        # 简洁输出
                        if event_type == "team_start":
                            print(f"  → team_start: members={event.get('members')}")
                        elif event_type == "team_status":
                            print(f"  → team_status: phase={event.get('phase')}")
                        elif event_type == "team_task_update":
                            task = event.get("task", {})
                            print(f"  → task_update: [{task.get('id','?')[:8]}] {task.get('title','?')[:50]} status={task.get('status')}")
                        elif event_type == "team_end":
                            print(f"  → team_end: status={event.get('status')} rounds={event.get('total_rounds')}")
                        elif event_type == "team_error":
                            print(f"  → team_error: {event.get('content','?')[:100]}")
                    except json.JSONDecodeError:
                        pass
        
        # ── Step 6: 验证事件 ──
        print(f"\nStep 6: 验证 — 收到 {len(events_received)} 个事件")
        
        event_types = [e["type"] for e in events_received]
        print(f"  事件类型: {event_types}")
        
        # 必须有 team_start
        assert any(e["type"] == "team_start" for e in events_received), "Missing team_start"
        print("  ✓ team_start 事件存在")
        
        # 必须有 team_end
        assert any(e["type"] == "team_end" for e in events_received), "Missing team_end"
        print("  ✓ team_end 事件存在")
        
        # team_end status 应为 completed/cancelled/error 之一
        end_event = next(e for e in events_received if e["type"] == "team_end")
        assert end_event["status"] in ("completed", "cancelled", "error"), \
            f"Unexpected team_end status: {end_event['status']}"
        print(f"  ✓ team_end status = {end_event['status']}")
        
        # ── Step 7: 清理 ──
        print("\nStep 7: 清理测试数据")
        for agent_name in ["e2e_lead", "e2e_writer"]:
            await client.delete(
                f"{HARNESS_URL}/api/v1/agents/{agent_name}",
                params={"user_id": USER_ID},
            )
            print(f"  ✓ Agent '{agent_name}' deleted")
        
        await client.delete(
            f"{HARNESS_URL}/api/v1/projects/{project_id}",
            params={"user_id": USER_ID},
        )
        print(f"  ✓ Project '{project_id}' deleted")
        
        print("\n" + "=" * 60)
        print("全部 E2E 测试通过! ✅")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
```

运行方式：

```bash
cd /mnt/d/Langchain_study/开源agent/multiagent-studio
python harness/team/tests/test_e2e.py
```

### 4.2 边界条件测试脚本

```python
"""边界条件 E2E 测试."""

async def test_empty_project_degrade():
    """空项目 Team 模式应正常结束（不崩溃）."""
    # 创建无成员的项目
    # 发送 mode=team 请求
    # 验证: 收到 team_start + team_end (status=completed)
    # 验证: 无异常抛出
    pass

async def test_cancel_during_execution():
    """执行中取消."""
    # 发送 mode=team 请求
    # 2 秒后调用 POST /stop/{thread_id}
    # 验证: team_end status=cancelled
    pass

async def test_nonexistent_project():
    """使用不存在的 project_id."""
    # 验证: team_degrade 事件 + 降级为单 Agent
    pass

async def test_member_init_failure_isolation():
    """单个 member 初始化失败不影响其他 member."""
    # 创建项目包含一个有效 Agent + 一个不存在的 Agent
    # 验证: 有效 Agent 正常初始化, 不存在 Agent 标记 failed
    pass
```

---

## 五、Layer 4：利用现有前端页面测试

虽然前端尚未适配 Team 模式 UI，但可以利用现有前端页面 + 浏览器 DevTools 进行部分验证。

### 5.1 可验证的内容

| 操作 | 路径 | 验证点 |
|---|---|---|
| 创建 Agent | `/agents/new` | 新字段（memory_scope 等）是否出现在 API payload 中 |
| Agent 列表 | `/agents` | 新创建的 Agent 是否正确显示 |
| 创建项目 | `/projects` | 创建项目、添加成员 |
| 项目详情 | `/projects/[id]` | Members 标签页显示成员列表 |

### 5.2 浏览器 Network 面板监控

1. 打开浏览器 DevTools → Network 标签
2. 创建 Agent 时，查看 POST `/api/v1/agents` 的 payload：
   - 确认 `memory_scope`、`can_be_lead`、`can_delegate` 等新字段被发送
   - 如果前端未更新，payload 可能不含这些字段（使用默认值）
3. 创建 Thread 时，查看 POST `/api/threads` 的 payload：
   - 确认 `project_id`、`agent_name`、`mode` 字段被发送

### 5.3 使用 curl 模拟前端行为

```bash
# 完整模拟：创建 Thread → 发送消息 → 接收 SSE

THREAD_ID="manual-test-$(uuidgen | cut -c1-8)"

# 1. 创建 Team Thread
curl -s -X POST http://localhost:8000/api/threads \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{
    \"title\": \"手动 Team 测试\",
    \"project_id\": \"$PROJECT_ID\",
    \"mode\": \"team\"
  }"

# 2. 发送消息 + 监听 SSE
curl -s -N -X POST http://localhost:8000/api/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{
    \"thread_id\": \"$THREAD_ID\",
    \"message\": \"请帮我分析这个项目的代码结构\",
    \"project_id\": \"$PROJECT_ID\",
    \"mode\": \"team\"
  }" | while IFS= read -r line; do
    if [[ $line == data:* ]]; then
      echo "$line" | python -c "
import json, sys
line = sys.stdin.read().strip()
if line.startswith('data: '):
    event = json.loads(line[6:])
    t = event.get('type', '?')
    if t == 'message':
        print(event.get('content', ''), end='', flush=True)
    elif t != 'message' and t != 'thinking':
        print(f'\n[{t}] {json.dumps(event, ensure_ascii=False)[:200]}')
"
    fi
  done
```

---

## 六、测试数据准备脚本

为了方便反复测试，准备一个一键初始化脚本：

```bash
#!/bin/bash
# setup_test_fixtures.sh — 创建测试用的 Agent 和 Project

HARNESS_URL="${HARNESS_URL:-http://localhost:8001}"
USER_ID="${USER_ID:-default}"

echo "=== 创建测试 Agent ==="

# Lead Agent
curl -s -X POST "$HARNESS_URL/api/v1/agents" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "pm_lead",
    "display_name": "项目经理 PM",
    "description": "负责需求分析、任务拆分和团队协调",
    "soul": "# 角色\n你是项目经理。擅长将模糊需求拆分为可执行的任务。\n\n## 工作方式\n1. 先理解目标\n2. 拆分为独立子任务\n3. 按优先级排序\n4. 分配给团队成员\n\n## 工具使用\n- 使用 task_create 创建任务\n- 使用 delegate_to_member 分配任务\n- 使用 task_list 查看进度\n- 使用 review_task 审阅结果",
    "model": "inherit",
    "tool_groups": ["files", "search"],
    "memory_scope": "project",
    "can_be_lead": true,
    "can_delegate": true,
    "max_turns": 30,
    "timeout_seconds": 900,
    "isolation": "none",
    "user_id": "'$USER_ID'"
  }' | python -m json.tool

# Coder Agent
curl -s -X POST "$HARNESS_URL/api/v1/agents" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dev_coder",
    "display_name": "开发工程师",
    "description": "负责代码编写、调试和代码审查",
    "soul": "# 角色\n你是资深后端开发工程师。\n\n## 工作方式\n1. 收到任务后先理解需求\n2. 不清晰时用 send_message 向 PM 提问\n3. 完成后用 task_update 更新状态",
    "model": "inherit",
    "tool_groups": ["files", "code", "shell"],
    "memory_scope": "team",
    "can_be_lead": false,
    "can_delegate": false,
    "max_turns": 25,
    "timeout_seconds": 600,
    "isolation": "none",
    "user_id": "'$USER_ID'"
  }' | python -m json.tool

# Researcher Agent
curl -s -X POST "$HARNESS_URL/api/v1/agents" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "researcher_ai",
    "display_name": "研究员",
    "description": "负责信息搜索、资料整理和报告撰写",
    "soul": "# 角色\n你是信息检索和分析专家。\n\n## 工作方式\n1. 收到搜索任务后多方查找\n2. 交叉验证信息来源\n3. 输出结构化的研究报告",
    "model": "inherit",
    "tool_groups": ["search", "files"],
    "memory_scope": "team",
    "can_be_lead": false,
    "can_delegate": false,
    "max_turns": 20,
    "timeout_seconds": 600,
    "isolation": "none",
    "user_id": "'$USER_ID'"
  }' | python -m json.tool

echo ""
echo "=== 创建测试项目 ==="

PROJECT_RESP=$(curl -s -X POST "$HARNESS_URL/api/v1/projects" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "测试团队项目",
    "description": "Agent Team 功能测试专用项目",
    "members": [],
    "user_id": "'$USER_ID'"
  }')

echo "$PROJECT_RESP" | python -m json.tool
PROJECT_ID=$(echo "$PROJECT_RESP" | python -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo ""
echo "Project ID: $PROJECT_ID"

echo ""
echo "=== 添加成员 ==="

for agent in "pm_lead" "dev_coder" "researcher_ai"; do
  echo -n "添加 $agent ... "
  curl -s -X POST "$HARNESS_URL/api/v1/projects/$PROJECT_ID/members" \
    -H "Content-Type: application/json" \
    -d "{\"agent_name\": \"$agent\", \"user_id\": \"$USER_ID\"}" | python -c "import json,sys; d=json.load(sys.stdin); print(f'成员数: {len(d.get(\"members\",[]))}')"
done

echo ""
echo "=== 清理命令 ==="
echo "# 删除项目: curl -X DELETE $HARNESS_URL/api/v1/projects/$PROJECT_ID?user_id=$USER_ID"
echo "# 删除 Agent:"
for agent in "pm_lead" "dev_coder" "researcher_ai"; do
  echo "#   curl -X DELETE $HARNESS_URL/api/v1/agents/$agent?user_id=$USER_ID"
done
```

使用方式：

```bash
chmod +x setup_test_fixtures.sh
./setup_test_fixtures.sh
```

---

## 七、关键验证清单

### 7.1 数据模型层

- [ ] Thread 表包含 `project_id`、`agent_name`、`mode` 列
- [ ] AgentConfig 包含 `memory_scope`、`can_be_lead`、`can_delegate`、`max_turns` 等新字段
- [ ] ExecuteRequest 接受 `project_id`、`agent_name`、`mode`
- [ ] 默认值：不传 `mode` 时默认为 `"single"`
- [ ] 创建不绑定项目的 Thread 时 `project_id` 为 `None`

### 7.2 Team 引擎层

- [ ] 创建 Project → 添加 Member → `initialize()` 成功
- [ ] 项目无成员时 `initialize()` 不抛异常，`run()` 立即结束
- [ ] 用户消息被创建为首个 PENDING 任务
- [ ] `get_ready_tasks()` 正确解析依赖 DAG
- [ ] `check_circular_dependency()` 检测到依赖环
- [ ] 任务重试 3 次后标记 FAILED
- [ ] `_select_idle_agent()` 负载均衡：优先轻负载 member

### 7.3 SSE 事件流

- [ ] `team_start` 包含 `project_id`、`members`、`mode`
- [ ] `team_task_update` 在任务创建/状态变更时触发
- [ ] `member_status` 在 member 状态变更时触发
- [ ] `team_end` 包含 `status` 和 `total_rounds`
- [ ] Watchdog 检测到超时/死锁时触发 `team_error`
- [ ] 取消执行时 `team_end` status=`cancelled`

### 7.4 边界条件

- [ ] 不存在的 `project_id` → `team_degrade` 降级事件
- [ ] 项目无成员 → 正常结束（不崩溃）
- [ ] 执行中停止 → `cancelled` 状态
- [ ] Member 执行崩溃 → 任务 FAILED，member 恢复 idle
- [ ] 同时分配同一任务 → 第二次分配返回 error
- [ ] 依赖环 → watchdog 检测并终止
- [ ] 30 分钟超时 → watchdog 触发终止

### 7.5 向后兼容

- [ ] `mode=single`（默认）的所有现有行为不变
- [ ] 不传 `project_id` 时走单 Agent 路径
- [ ] 现有测试全部通过

---

## 八、快速冒烟测试

最小化的验证步骤（5 分钟内完成）：

```bash
# 1. 运行单元测试
python -m pytest harness/team/tests/ -v

# 2. 验证 Agent 创建（新字段）
curl -s -X POST http://localhost:8001/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"smoke_test","display_name":"smoke","soul":"test","model":"inherit","tool_groups":[],"memory_scope":"team","can_be_lead":false,"max_turns":10,"timeout_seconds":300,"user_id":"default"}' \
  | python -c "import json,sys; r=json.load(sys.stdin); assert r['status']=='created'; print('✓ Agent created')"

# 3. 验证 Project 创建
PROJ=$(curl -s -X POST http://localhost:8001/api/v1/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"smoke","members":["smoke_test"],"user_id":"default"}')
PROJ_ID=$(echo $PROJ | python -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "✓ Project created: $PROJ_ID"

# 4. 验证 Team 执行（不需要真实 LLM — 只验证路由）
curl -s -N -X POST http://localhost:8001/api/v1/execute \
  -H "Content-Type: application/json" \
  -d "{\"thread_id\":\"smoke-$RANDOM\",\"user_id\":\"default\",\"message\":\"test\",\"project_id\":\"$PROJ_ID\",\"mode\":\"team\"}" \
  2>&1 | head -20

# 5. 清理
curl -s -X DELETE "http://localhost:8001/api/v1/agents/smoke_test?user_id=default"
curl -s -X DELETE "http://localhost:8001/api/v1/projects/$PROJ_ID?user_id=default"
echo "✓ Cleanup done"
```
