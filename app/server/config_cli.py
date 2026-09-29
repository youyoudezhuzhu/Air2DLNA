#!/usr/bin/env python3
"""配置命令行工具，供飞牛生命周期脚本调用。

用法::

    config_cli.py --config-dir DIR init                  # 若不存在则写入默认配置
    config_cli.py --config-dir DIR set k=v [k=v ...]     # 校验并更新（非法项整体拒绝）
    config_cli.py --config-dir DIR migrate               # 幂等迁移（补齐/规范化字段）

退出码：0 成功；1 失败（原因写 stderr）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air2dlna.config import Config, ConfigError, migrate  # noqa: E402


def _load_raw(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Air2DLNA 配置工具")
    parser.add_argument("--config-dir", required=True, help="配置目录（TRIM_PKGETC）")
    parser.add_argument("action", choices=("init", "set", "migrate"))
    parser.add_argument("assignments", nargs="*", help="k=v 形式")
    args = parser.parse_args(argv)

    os.makedirs(args.config_dir, exist_ok=True)
    path = os.path.join(args.config_dir, "config.json")

    if args.action == "init":
        if os.path.exists(path):
            # 已存在：只做迁移，绝不覆盖用户设置
            config = Config(path)
            config.update({}, persist=True)
            print("配置已存在，仅执行迁移")
            return 0
        config = Config(path)
        config.save()
        print(f"已写入默认配置: {path}")
        return 0

    if args.action == "migrate":
        raw = _load_raw(path)
        result = migrate(raw)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"配置迁移完成: {path}")
        return 0

    # set
    changes: dict[str, str] = {}
    for assignment in args.assignments:
        if "=" not in assignment:
            print(f"参数格式错误（应为 k=v）: {assignment}", file=sys.stderr)
            return 1
        key, value = assignment.split("=", 1)
        key = key.strip()
        if key.startswith("wizard_"):
            # 向导字段前缀去掉，映射到真实配置键
            key = key[len("wizard_"):]
        if not key:
            continue
        if value == "":
            # 向导留空表示「保持默认」，不覆盖已有值
            continue
        changes[key] = value
    if not changes:
        print("没有需要更新的配置项")
        return 0
    try:
        config = Config(path)
        config.update(changes)
    except ConfigError as exc:
        print(f"配置校验失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"配置写入失败: {exc}", file=sys.stderr)
        return 1
    print(f"配置已更新: {', '.join(sorted(changes))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
