"""Alembic 迁移环境配置"""

import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic import context

# 加载 SQLModel 元数据
from sqlmodel import SQLModel
from app.models import User, Thread, Message, FileRecord, UserConfig  # noqa: F401

# 从项目根 .env 读取 DATABASE_URL_SYNC（alembic.ini 的 ${} 占位在此展开）
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = os.getenv("DATABASE_URL_SYNC") or config.get_main_option("sqlalchemy.url")
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
