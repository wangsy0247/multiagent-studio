#!/usr/bin/env python3
"""End-to-end test: Agent Team orchestration flow.

Creates an orchestration agent, project, adds members, then runs team execution.

Usage:
    python test_team_e2e.py
"""

import requests
import json
import sys

APP_URL = "http://localhost:8000"
HARNESS_URL = "http://localhost:8001"

# ── test data ──
TEST_EMAIL = "test_e2e@example.com"
TEST_PASSWORD = "test123456"
TEST_USERNAME = "test_e2e_user"
AGENT_NAME = "orchestrator"
PROJECT_NAME = "test-project-e2e"


def main():
    s = requests.Session()

    # ── Step 1: Register or Login ──
    print("=" * 50)
    print("Step 1: Auth")
    resp = s.post(f"{APP_URL}/api/auth/register", json={
        "email": TEST_EMAIL, "username": TEST_USERNAME,
        "password": TEST_PASSWORD, "display_name": "E2E Test",
    })
    if resp.status_code == 400 and "已存在" in resp.json().get("detail", ""):
        print("  User exists, logging in...")
        resp = s.post(f"{APP_URL}/api/auth/login", json={
            "email": TEST_EMAIL, "password": TEST_PASSWORD,
        })
    if resp.status_code != 200 and resp.status_code != 201:
        print(f"  Auth FAILED: {resp.status_code} {resp.text}")
        sys.exit(1)
    token_data = resp.json()
    token = token_data.get("access_token", "")
    s.headers["Authorization"] = f"Bearer {token}"
    print(f"  Logged in, token={token[:20]}...")

    # ── Step 2: Get user info ──
    resp = s.get(f"{APP_URL}/api/auth/me")
    user = resp.json()
    user_id = user.get("id", "default")
    print(f"  user_id={user_id}")

    # ── Step 3: Create orchestrator agent ──
    print("=" * 50)
    print("Step 2: Create orchestrator agent")
    resp = s.post(f"{APP_URL}/api/v1/agents", json={
        "name": AGENT_NAME,
        "display_name": "Orchestrator",
        "description": "Team lead agent for E2E testing",
        "soul": "# Orchestrator\n\nYou are a team orchestrator. Break down goals into tasks.",
        "model": "inherit",
        "tool_groups": [],
        "memory_scope": "project",
        "can_be_lead": True,
        "can_delegate": True,
        "max_turns": 50,
        "timeout_seconds": 900,
        "isolation": "none",
        "user_id": user_id,
    })
    if resp.status_code not in (200, 201):
        print(f"  Create agent FAILED: {resp.status_code} {resp.text}")
    else:
        print(f"  Agent '{AGENT_NAME}' created: {resp.json()}")

    # ── Step 4: Create second member agent ──
    resp = s.post(f"{APP_URL}/api/v1/agents", json={
        "name": "coder_e2e",
        "display_name": "Coder Bot",
        "description": "Coding specialist",
        "soul": "# Coder\n\nYou write code.",
        "model": "inherit",
        "tool_groups": [],
        "memory_scope": "project",
        "can_be_lead": False,
        "can_delegate": False,
        "max_turns": 30,
        "timeout_seconds": 600,
        "isolation": "none",
        "user_id": user_id,
    })
    print(f"  Agent 'coder_e2e': status={resp.status_code}")

    # ── Step 5: Create project ──
    print("=" * 50)
    print("Step 3: Create project")
    resp = s.post(f"{APP_URL}/api/v1/projects", json={
        "name": PROJECT_NAME,
        "description": "E2E test project for team orchestration",
        "members": [AGENT_NAME, "coder_e2e"],
        "user_id": user_id,
    })
    if resp.status_code not in (200, 201):
        print(f"  Create project FAILED: {resp.status_code} {resp.text}")
        sys.exit(1)
    project = resp.json()
    project_id = project.get("id", "")
    print(f"  Project created: id={project_id}, members={project.get('members')}")

    # ── Step 6: Create thread ──
    print("=" * 50)
    print("Step 4: Create thread")
    resp = s.post(f"{APP_URL}/api/threads", json={
        "title": "Team E2E Test",
        "project_id": project_id,
        "agent_name": AGENT_NAME,
        "mode": "team",
    })
    if resp.status_code not in (200, 201):
        print(f"  Create thread FAILED: {resp.status_code} {resp.text}")
        sys.exit(1)
    thread = resp.json()
    thread_id = thread.get("id", "")
    print(f"  Thread created: id={thread_id}")

    # ── Step 7: Execute in team mode ──
    print("=" * 50)
    print("Step 5: Execute team mode")
    payload = {
        "thread_id": thread_id,
        "user_id": user_id,
        "message": "请规划一个简单的 Python 项目，创建目录结构和一个 main.py 入口文件。",
        "project_id": project_id,
        "agent_name": AGENT_NAME,
        "mode": "team",
    }
    print(f"  Sending: {json.dumps(payload, ensure_ascii=False)[:200]}...")

    resp = s.post(f"{APP_URL}/api/execute", json=payload, stream=True)
    print(f"  Response status: {resp.status_code}")

    event_count = 0
    has_team_start = False
    has_team_end = False
    has_degrade = False
    error_events = []

    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            event_count += 1
            try:
                event = json.loads(line[6:])
                etype = event.get("type", "?")
                if etype == "team_start":
                    has_team_start = True
                    print(f"  ✓ team_start: members={event.get('members')}")
                elif etype == "team_end":
                    has_team_end = True
                    print(f"  ✓ team_end: status={event.get('status')}, rounds={event.get('total_rounds')}")
                elif etype == "team_degrade":
                    has_degrade = True
                    print(f"  ⚠ team_degrade: reason={event.get('reason')}")
                elif etype == "team_error":
                    error_events.append(event)
                    print(f"  ✗ team_error: {event.get('content', event.get('reason', ''))[:200]}")
                elif etype in ("team_status", "team_task_update", "member_status", "team_message"):
                    # Summary for progress events
                    if etype == "team_status":
                        print(f"  → {event.get('phase')}: {event.get('content', '')[:80]}")
                else:
                    # Single-agent fallback events
                    if event_count <= 3:
                        print(f"  [{etype}] {str(event.get('content', ''))[:80]}")
            except json.JSONDecodeError:
                pass
            if event_count > 500:
                print("  ... (truncated after 500 events)")
                break

    print(f"\n  Total events: {event_count}")
    print(f"  has_team_start={has_team_start}, has_team_end={has_team_end}")

    # ── Summary ──
    print("=" * 50)
    print("E2E Test Summary:")
    print(f"  user_id:        {user_id}")
    print(f"  project_id:     {project_id}")
    print(f"  thread_id:      {thread_id}")
    print(f"  Agent dir:      ~/.multiagent-studio/users/{user_id}/agents/{AGENT_NAME}/")
    print(f"  Project file:   ~/.multiagent-studio/users/{user_id}/projects/{project_id}.json")
    if has_team_start and has_team_end:
        print("  Result: ✅ Team orchestration worked!")
    elif has_degrade:
        print("  Result: ⚠️  Team degraded to single-agent mode")
    elif error_events:
        for e in error_events:
            print(f"  Error: {e.get('content', e.get('reason', ''))[:200]}")
        print("  Result: ❌ Team execution had errors (see above)")
    else:
        print("  Result: ❓ Unexpected — check Harness logs")


if __name__ == "__main__":
    main()
