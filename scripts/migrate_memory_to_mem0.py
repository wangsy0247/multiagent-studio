"""一次性迁移脚本：把现有 memory.json 的 facts/summaries 迁移到 mem0。

Usage::

    python scripts/migrate_memory_to_mem0.py [--memory-root ~/.multiagent-studio]

迁移前请确保：
1. config.yaml 中 memory.backend 已设为 mem0
2. mem0_config 中的 llm/embedder 配置正确
3. mem0ai 和 chromadb 已安装
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def migrate(memory_root: str = "~/.multiagent-studio") -> None:
    """迁移所有用户的 memory.json 到 mem0。

    对每个用户：
    1. 读取 {memory_root}/users/{user_id}/memory.json
    2. 将 facts 逐条写入 mem0.add()
    3. 将 summaries 逐条写入 mem0.add()
    """
    from harness.memory.mem0_client import get_mem0, is_mem0_enabled

    # if not is_mem0_enabled():
    #     print("❌ mem0 backend 未启用，请先在 config.yaml 中设置 memory.backend=mem0")
    #     return

    mem0 = get_mem0()
    if mem0 is None:
        print("❌ mem0 客户端初始化失败，请检查 mem0_config 配置")
        return

    root = Path(memory_root).expanduser()
    users_dir = root / "users"
    if not users_dir.exists():
        print(f"❌ 用户目录不存在: {users_dir}")
        return

    total_users = 0
    total_facts = 0
    total_summaries = 0
    errors = 0

    for user_dir in sorted(users_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        user_id = user_dir.name
        mem_file = user_dir / "memory.json"
        if not mem_file.exists():
            print(f"  ⏭  {user_id}: 没有 memory.json，跳过")
            continue

        try:
            data = json.loads(mem_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠  {user_id}: 读取 memory.json 失败: {e}")
            errors += 1
            continue

        user_facts = 0
        user_summaries = 0

        # ── 迁移 facts ──
        facts = data.get("facts", [])
        for fact in facts:
            content = fact.get("content", "")
            if not content or not isinstance(content, str) or not content.strip():
                continue

            # 构建 metadata
            metadata: dict = {
                "migrated_from": "file_storage",
                "category": fact.get("category", ""),
                "confidence": fact.get("confidence", 0.8),
            }
            # 如果有 createdAt/updatedAt，作为 event_time
            created = fact.get("createdAt") or fact.get("updatedAt")
            if created and isinstance(created, str):
                metadata["event_time"] = created

            try:
                mem0.add(
                    f"用户事实：{content.strip()}",
                    user_id=user_id,
                    metadata=metadata,
                )
                user_facts += 1
            except Exception as e:
                print(f"    ⚠  迁移 fact 失败 ({user_id}): {e}")
                errors += 1

        # ── 迁移 summaries ──
        for section in ("user", "history"):
            section_data = data.get(section, {})
            if not isinstance(section_data, dict):
                continue
            for key, val in section_data.items():
                if not isinstance(val, dict):
                    continue
                summary = val.get("summary", "")
                if not summary or not isinstance(summary, str) or not summary.strip():
                    continue
                try:
                    mem0.add(
                        f"[{section}.{key}] {summary.strip()}",
                        user_id=user_id,
                        metadata={
                            "migrated_from": "file_storage",
                            "type": "summary",
                            "section": f"{section}.{key}",
                            "updated_at": val.get("updatedAt", ""),
                        },
                    )
                    user_summaries += 1
                except Exception as e:
                    print(f"    ⚠  迁移 summary 失败 ({user_id}): {e}")
                    errors += 1

        total_facts += user_facts
        total_summaries += user_summaries
        total_users += 1
        print(
            f"  ✅ {user_id}: {user_facts} facts + {user_summaries} summaries 已迁移"
        )

    print()
    print(f"迁移完成: {total_users} 用户, {total_facts} facts, "
          f"{total_summaries} summaries, {errors} 错误")
    print()
    print("迁移后建议：")
    print("1. 备份原 memory.json 文件（不要立即删除）")
    print("2. 确认 mem0 检索正常工作后，可归档原 JSON 文件")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="将 FileMemoryStorage 的 memory.json 迁移到 mem0",
    )
    parser.add_argument(
        "--memory-root",
        default="~/.multiagent-studio/memory",
        help="Memory 根目录路径（默认: ~/.multiagent-studio/memory）",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.memory_root))
