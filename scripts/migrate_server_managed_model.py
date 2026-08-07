"""存量配置迁移: 模型 API 上收服务器后, 清理用户 YAML 中的模型字段.

背景:
    模型配置 (api_key / base_url / model / 辅助模型) 改由服务器统一注入
    (harness/.env → 环境变量 → L0 SYSTEM_DEFAULTS 插值 → SERVER_FORCED_KEYS
    强制覆盖)。用户全局 config.yaml 和 agent config.yaml 中残留的同名字段
    已经失效 (merge 后被强制覆盖), 本脚本只做清理, 让配置文件与实际行为一致。

用法:
    python scripts/migrate_server_managed_model.py            # dry-run (默认)
    python scripts/migrate_server_managed_model.py --apply    # 实际写入 (先备份)

清理内容:
    users/*/config.yaml        → 删除 api_key, base_url, default_model,
                                 summary_model, title_model, memory_model
    users/*/agents/*/config.yaml → 删除 model
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

# 用户全局 config.yaml 中待删除的模型字段
USER_GLOBAL_KEYS = (
    "api_key",
    "base_url",
    "default_model",
    "summary_model",
    "title_model",
    "memory_model",
)
# agent config.yaml 中待删除的字段
AGENT_KEYS = ("model",)


def _default_data_root() -> Path:
    try:
        from harness.config.paths import get_paths
        return Path(get_paths().base_dir)
    except Exception:
        return Path("~/.multiagent-studio").expanduser()


def _clean_file(path: Path, keys: tuple[str, ...], *, apply: bool, backup_suffix: str) -> list[str]:
    """删除 YAML 中的指定 key, 返回实际删除的 key 列表."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [跳过] {path}: 解析失败 ({exc})")
        return []
    if not isinstance(data, dict):
        return []

    removed = [k for k in keys if k in data]
    if not removed:
        return []

    if apply:
        backup = path.with_suffix(path.suffix + backup_suffix)
        shutil.copy2(path, backup)
        for k in removed:
            data.pop(k, None)
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="实际写入 (默认 dry-run)")
    parser.add_argument("--data-root", type=Path, default=None, help="数据根目录 (默认 ~/.multiagent-studio)")
    args = parser.parse_args()

    data_root = (args.data_root or _default_data_root()).expanduser()
    users_dir = data_root / "users"
    if not users_dir.is_dir():
        print(f"用户目录不存在: {users_dir}")
        return 1

    backup_suffix = f".bak.{datetime.now():%Y%m%d%H%M%S}"
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] data_root={data_root}")

    changed = 0
    for user_dir in sorted(users_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        # 用户全局 config.yaml
        cfg = user_dir / "config.yaml"
        if cfg.exists():
            removed = _clean_file(cfg, USER_GLOBAL_KEYS, apply=args.apply, backup_suffix=backup_suffix)
            if removed:
                changed += 1
                print(f"  [{user_dir.name}] config.yaml 删除: {', '.join(removed)}")
        # agent config.yaml
        agents_dir = user_dir / "agents"
        if agents_dir.is_dir():
            for agent_dir in sorted(agents_dir.iterdir()):
                acfg = agent_dir / "config.yaml"
                if not acfg.exists():
                    continue
                removed = _clean_file(acfg, AGENT_KEYS, apply=args.apply, backup_suffix=backup_suffix)
                if removed:
                    changed += 1
                    print(f"  [{user_dir.name}/{agent_dir.name}] config.yaml 删除: {', '.join(removed)}")

    if changed == 0:
        print("没有需要清理的配置。")
    elif not args.apply:
        print(f"\n共 {changed} 个文件待清理 (dry-run)。加 --apply 实际执行 (会先备份 *{backup_suffix})。")
    else:
        print(f"\n已清理 {changed} 个文件, 备份后缀: {backup_suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
