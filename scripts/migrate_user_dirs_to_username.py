#!/usr/bin/env python3
"""将 users/{uuid} 目录迁移合并到 users/{username}（统一文件系统用户标识）.

背景:
    历史上注册/执行链路使用 users.id (uuid) 作为文件系统目录名，
    而前端 Agent/项目管理使用 username，导致同一用户的数据分裂在两个目录。
    现在全链路统一为 username，本脚本把存量 uuid 目录合并进 username 目录。

合并策略:
    - 目标文件不存在 → 直接移动 (目录取并集)
    - 目标文件已存在 → 保留 username 版本 (前端编辑产物)，
      uuid 版本保存为 目标路径 + ".uuid-conflict.bak"
    - 合并完成后删除空的 uuid 目录
    - 不匹配任何数据库用户的目录 (测试目录、已删除用户的残留) 只报告，不动

默认 dry-run，加 --apply 才真正执行:

    python scripts/migrate_user_dirs_to_username.py            # 预览
    python scripts/migrate_user_dirs_to_username.py --apply    # 执行

用户表来源 (优先级): --db-url 参数 > 环境变量 DATABASE_URL_SYNC
    > 项目 .env 中的 DATABASE_URL_SYNC > <data-root>/app.db (SQLite)

注意:
    file 后端的 memory.json 存放在用户目录内，随目录自动迁移；
    mem0 向量库 (pgvector/chroma) 中按旧 uuid 存储的记忆不在本脚本范围内，
    如有需要请另行处理 (或接受该部分记忆不可见)。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import uuid as uuid_mod
from pathlib import Path

SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
CONFLICT_SUFFIX = ".uuid-conflict.bak"


def _normalize_uuid(raw: str) -> str:
    """SQLite 中 uuid 以 32 位无横线 hex 存储，目录名是带横线形式。"""
    return str(uuid_mod.UUID(str(raw).replace("-", "")))


def load_users_sqlite(db_path: Path) -> list[tuple[str, str]]:
    """从 SQLite app.db 读取 (dashed_uuid, username) 列表."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, username FROM users").fetchall()
    finally:
        con.close()
    return [(_normalize_uuid(rid), uname) for rid, uname in rows]


def load_users_pg(db_url: str) -> list[tuple[str, str]]:
    """从 PostgreSQL (或其他 SQLAlchemy 支持的库) 读取用户列表."""
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        sys.exit("需要 SQLAlchemy + 对应驱动来连接: " + db_url)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, username FROM users")).fetchall()
    finally:
        engine.dispose()
    return [(_normalize_uuid(rid), uname) for rid, uname in rows]


def _env_from_dotenv(key: str) -> str:
    """从项目根目录 .env 读取某个 key (简单解析, 不依赖 python-dotenv)."""
    dotenv = Path(__file__).resolve().parent.parent / ".env"
    if not dotenv.exists():
        return ""
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return ""


def merge_tree(src: Path, dst: Path, *, apply: bool) -> tuple[int, int]:
    """把 src 合并进 dst。返回 (moved, conflicts) 文件计数."""
    moved = conflicts = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        for name in files:
            s = Path(root) / name
            d = dst / rel / name
            if d.exists():
                backup = d.with_name(d.name + CONFLICT_SUFFIX)
                conflicts += 1
                print(f"  CONFLICT {d}  (保留 username 版本, uuid 版本 → {backup.name})")
                if apply:
                    shutil.move(str(s), str(backup))
            else:
                moved += 1
                print(f"  MOVE {s} → {d}")
                if apply:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(s), str(d))
    return moved, conflicts


def remove_empty_dirs(path: Path, *, apply: bool) -> bool:
    """自底向上删除空目录。返回 path 是否已完全移除."""
    if not path.exists():
        return True
    for root, dirs, _files in os.walk(path, topdown=False):
        for d in dirs:
            p = Path(root) / d
            try:
                p.rmdir() if apply else None
            except OSError:
                pass
    try:
        path.rmdir() if apply else None
    except OSError:
        return False
    return not path.exists() if apply else True


def migrate_one(users_root: Path, dashed_uuid: str, username: str, *, apply: bool) -> str:
    src = users_root / dashed_uuid
    dst = users_root / username

    if not SAFE_USER_ID_RE.match(username):
        return f"SKIP  {dashed_uuid} → {username!r}: 用户名含非法字符，跳过"
    if not src.exists():
        return f"OK    {dashed_uuid} → {username}: 无 uuid 目录 (已迁移或从未创建)"
    if src == dst:
        return f"OK    {username}: 目录名已是 username"

    print(f"MERGE {dashed_uuid} → {username}:")
    moved, conflicts = merge_tree(src, dst, apply=apply)
    removed = remove_empty_dirs(src, apply=apply)
    status = "完成" if removed else "完成 (源目录仍有残留，请人工检查)"
    return f"DONE  {username}: 移动 {moved} 个文件, {conflicts} 个冲突 — {status}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", default="~/.multiagent-studio",
                        help="Harness 数据根目录 (默认 ~/.multiagent-studio)")
    parser.add_argument("--db", default=None,
                        help="SQLite app.db 路径 (优先级高于 DATABASE_URL_SYNC)")
    parser.add_argument("--db-url", default=None,
                        help="同步 SQLAlchemy 数据库 URL, 如 postgresql://user:pass@host:5432/db")
    parser.add_argument("--apply", action="store_true", help="实际执行 (默认 dry-run)")
    args = parser.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    users_root = data_root / "users"

    if not users_root.exists():
        print(f"users 目录不存在: {users_root} — 无需迁移")
        return 0

    # ── 解析用户表来源 ──
    db_url = args.db_url or os.getenv("DATABASE_URL_SYNC") or _env_from_dotenv("DATABASE_URL_SYNC")
    sqlite_path = Path(args.db).expanduser().resolve() if args.db else data_root / "app.db"

    users: list[tuple[str, str]] | None = None
    if args.db:
        if not sqlite_path.exists():
            print(f"数据库不存在: {sqlite_path}", file=sys.stderr)
            return 1
        users = load_users_sqlite(sqlite_path)
        source = f"sqlite:{sqlite_path}"
    elif db_url:
        users = load_users_pg(db_url)
        source = db_url.split("@")[-1]  # 隐藏凭据
    elif sqlite_path.exists():
        users = load_users_sqlite(sqlite_path)
        source = f"sqlite:{sqlite_path}"
    else:
        print("未找到用户表来源: 无 DATABASE_URL_SYNC, 也无 SQLite app.db", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] data_root={data_root}  users_from={source}\n")
    if not users:
        print("数据库中没有用户。")
    for dashed_uuid, username in users:
        print(migrate_one(users_root, dashed_uuid, username, apply=args.apply))
        print()

    # 报告不匹配任何用户的目录 (孤儿/测试目录)
    known = {u for _, u in users} | {uid for uid, _ in users}
    orphans = sorted(
        d.name for d in users_root.iterdir()
        if d.is_dir() and d.name not in known
    )
    if orphans:
        print("以下目录不匹配任何数据库用户，未做处理 (多为测试/残留目录，可人工清理):")
        for name in orphans:
            print(f"  - {users_root / name}")

    if not args.apply:
        print("\n以上为预览。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
