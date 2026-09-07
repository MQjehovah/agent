#!/usr/bin/env python3
"""SQLite 在线备份：不中断运行，备份到 <db 目录>/backups，保留最近 N 份。

用法:
    python scripts/backup_db.py                    # 自动定位 data.db
    python scripts/backup_db.py --db /app/config/data.db --keep 14
    docker exec agent python scripts/backup_db.py   # 容器内

建议配 cron（示例，每天 03:00）:
    0 3 * * * cd <agent> && python scripts/backup_db.py >> logs/backup.log 2>&1
"""
import argparse
import os
import sqlite3
import datetime


def resolve_db() -> str:
    candidates = [
        os.environ.get("AGENT_DB_PATH", ""),
        os.path.join(os.path.expanduser("~"), "agent", "config", "data.db"),
        "config/data.db",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    raise SystemExit("找不到 data.db，请用 --db 指定")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args()
    db = os.path.abspath(args.db) if args.db else resolve_db()
    backup_dir = os.path.join(os.path.dirname(db), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"data.{stamp}.db")

    src = sqlite3.connect(db)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"[backup] {db} -> {dest} ({os.path.getsize(dest)} bytes)")

    # 保留最近 keep 份，删除更早
    files = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("data.") and f.endswith(".db"))
    for old in files[:-args.keep]:
        os.remove(os.path.join(backup_dir, old))
        print(f"[backup] 清理旧备份 {old}")


if __name__ == "__main__":
    main()
