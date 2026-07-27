"""
PostgreSQL → SQLite 数据迁移脚本

将 App DB 从 PostgreSQL 迁移到 ~/.multiagent-studio/app.db (SQLite)
"""

import sqlite3
import os
import sys

# ── 配置 ──
PG_DSN = "postgresql://harness:harness@localhost:5432/multiagent_studio"
SQLITE_PATH = os.path.join(os.path.expanduser("~"), ".multiagent-studio", "app.db")

try:
    import psycopg2
except ImportError:
    print("需要安装 psycopg2: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


def backup_sqlite(path: str):
    """备份现有 SQLite 文件"""
    import shutil
    backup = path + ".backup"
    if os.path.exists(path):
        shutil.copy2(path, backup)
        print(f"[备份] {path} → {backup}")


def migrate():
    pg_conn = psycopg2.connect(PG_DSN)
    pg_conn.autocommit = False
    pg_cur = pg_conn.cursor()

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.execute("PRAGMA foreign_keys = OFF")
    sqlite_cur = sqlite_conn.cursor()

    try:
        # ── 0. 补全 SQLite 缺失的列 ──
        print("[结构] 检查 SQLite 缺失列...")
        existing_cols = set()
        for row in sqlite_cur.execute("PRAGMA table_info(threads)"):
            existing_cols.add(row[1])

        additions = [
            ("project_id", "VARCHAR(50)"),
            ("agent_name", "VARCHAR(100)"),
            ("mode", "VARCHAR(20) DEFAULT 'single'"),
        ]
        for col_name, col_type in additions:
            if col_name not in existing_cols:
                sqlite_cur.execute(
                    f"ALTER TABLE threads ADD COLUMN {col_name} {col_type}"
                )
                print(f"  + threads.{col_name} {col_type}")
        sqlite_conn.commit()

        # 创建缺失的表
        missing_tables = []
        for row in sqlite_cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            missing_tables.append(row[0])

        if "scheduled_tasks" not in missing_tables:
            sqlite_cur.execute("""
                CREATE TABLE scheduled_tasks (
                    id CHAR(32) NOT NULL PRIMARY KEY,
                    user_id CHAR(32) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    prompt VARCHAR NOT NULL,
                    cron_expr VARCHAR(100),
                    recurring BOOLEAN NOT NULL,
                    timezone VARCHAR(50) NOT NULL,
                    next_run_at DATETIME,
                    expires_at DATETIME,
                    enabled BOOLEAN NOT NULL,
                    mode VARCHAR(20) NOT NULL,
                    project_id VARCHAR(50),
                    agent_name VARCHAR(100),
                    thread_strategy VARCHAR(20) NOT NULL,
                    thread_id CHAR(32),
                    last_run_at DATETIME,
                    last_status VARCHAR(20),
                    last_error VARCHAR(2000),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    created_by VARCHAR(20) NOT NULL DEFAULT 'user',
                    allow_silent BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                )
            """)
            sqlite_cur.execute("CREATE INDEX ix_scheduled_tasks_user_id ON scheduled_tasks(user_id)")
            print("  + 创建 scheduled_tasks 表")

        if "task_runs" not in missing_tables:
            sqlite_cur.execute("""
                CREATE TABLE task_runs (
                    id CHAR(32) NOT NULL PRIMARY KEY,
                    task_id CHAR(32) NOT NULL,
                    thread_id CHAR(32),
                    status VARCHAR(20) NOT NULL,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME,
                    error VARCHAR(2000),
                    summary VARCHAR(500),
                    seen BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id),
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                )
            """)
            sqlite_cur.execute("CREATE INDEX ix_task_runs_task_id ON task_runs(task_id)")
            sqlite_cur.execute("CREATE INDEX ix_task_runs_seen ON task_runs(seen)")
            print("  + 创建 task_runs 表")

        sqlite_conn.commit()

        # ── 1. 逐表迁移 ──
        tables = [
            "users",
            "threads",
            "messages",
            "file_records",
            "user_configurations",
            "scheduled_tasks",
            "task_runs",
        ]

        for table in tables:
            # 清空目标表
            sqlite_cur.execute(f"DELETE FROM {table}")
            sqlite_conn.commit()

            # 从 PostgreSQL 读取 (task_runs 用 started_at 排序)
            order_col = "created_at" if table != "task_runs" else "started_at"
            pg_cur.execute(f"SELECT * FROM {table} ORDER BY {order_col}")
            columns = [desc[0] for desc in pg_cur.description]
            rows = pg_cur.fetchall()

            if not rows:
                print(f"[{table}] 0 行, 跳过")
                continue

            # 构建 INSERT
            placeholders = ", ".join(["?" for _ in columns])
            col_names = ", ".join(columns)
            insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

            count = 0
            for row in rows:
                # 类型转换: PG → SQLite
                converted = []
                for i, val in enumerate(row):
                    col = columns[i]
                    if val is None:
                        converted.append(None)
                    elif hasattr(val, 'isoformat'):
                        # datetime → ISO string
                        converted.append(val.isoformat())
                    elif isinstance(val, bool):
                        # boolean → 0/1
                        converted.append(1 if val else 0)
                    elif isinstance(val, (dict, list)):
                        # JSON → string
                        import json
                        converted.append(json.dumps(val))
                    else:
                        converted.append(str(val) if not isinstance(val, (int, float, str)) else val)
                try:
                    sqlite_cur.execute(insert_sql, converted)
                    count += 1
                except Exception as exc:
                    print(f"  [ERROR] {table} row {count}: {exc}")
                    print(f"    columns: {columns}")
                    print(f"    values: {converted[:5]}...")
                    raise

            sqlite_conn.commit()
            print(f"[{table}] {count} 行已迁移")

        print("\n✓ 迁移完成")

    finally:
        pg_cur.close()
        pg_conn.close()
        sqlite_cur.close()
        sqlite_conn.close()


if __name__ == "__main__":
    backup_sqlite(SQLITE_PATH)
    migrate()
