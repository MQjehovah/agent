#!/usr/bin/env python3
"""SSO 双账号合并一次性迁移(服务器部署时手动执行一次, 不自动跑)。

背景: SSO 登录此前按 ``name=sub(工号)`` 查/建, 老账号 ``name=中文名``(如
uid1 name=季明清) 查不到 → 给同一人新建 ``name=工号`` 账号(如 uid34
name=202202100024)。本脚本把老账号改名工号并补 display_name, 再删除
无独立业务数据的重复账号。

执行内容(默认 dry-run, 加 --apply 才落库):
1. 确保 rbac_users.display_name 列存在(缺失则 ALTER 补上, 幂等);
2. uid=<老账号id>: name 中文 → 改 name=<工号>; 空 display_name → 补中文名;
   (保留 role/department/status/dingtalk 绑定/历史)
3. uid=<重复账号id>: 校验其名下无 messages/memories/usage_records 等业务数据
   后删除(含 rbac_user_identities), 有数据则中止提示人工处理。

用法:
    python scripts/merge_sso_accounts.py                          # dry-run 预览
    python scripts/merge_sso_accounts.py --apply                  # 真正执行
    python scripts/merge_sso_accounts.py --db /app/config/data.db --apply
    docker exec agent python scripts/merge_sso_accounts.py --apply
"""
import argparse
import os
import sqlite3


def resolve_db(db_arg: str) -> str:
    if db_arg:
        db = os.path.abspath(db_arg)
    else:
        candidates = [
            os.environ.get("AGENT_DB_PATH", ""),
            os.path.join(os.path.expanduser("~"), "agent", "config", "data.db"),
            "config/data.db",
        ]
        db = next((c for c in candidates if c and os.path.isfile(c)), "")
    if not db or not os.path.isfile(db):
        raise SystemExit("找不到 data.db，请用 --db 指定")
    return os.path.abspath(db)


def ensure_display_name_col(conn, apply: bool) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rbac_users)").fetchall()]
    if "display_name" in cols:
        return
    print("[migrate] rbac_users 缺 display_name 列 → ALTER 补上")
    if apply:
        conn.execute("ALTER TABLE rbac_users ADD COLUMN display_name TEXT DEFAULT ''")
        conn.commit()


def merge_old_account(conn, apply: bool, uid: int, work_id: str, zh_name: str) -> None:
    """老账号(name=中文) → name=工号 + display_name=中文(幂等)。"""
    row = conn.execute(
        "SELECT id, name, display_name, role, status FROM rbac_users WHERE id=?",
        (uid,),
    ).fetchone()
    if not row:
        print(f"[migrate] uid={uid} 不存在, 跳过")
        return
    cur_name, cur_display = row["name"], row["display_name"] or ""
    if cur_name == work_id and cur_display:
        print(f"[migrate] uid={uid} 已是工号账号(name={work_id}, display_name={cur_display}), 无需处理")
        return
    new_display = cur_display if (cur_display and cur_name != work_id) else (zh_name or cur_display)
    print(f"[migrate] 合并 uid={uid}: name {cur_name!r} → {work_id!r}, display_name={new_display!r} "
          f"(保留 role={row['role']}/status={row['status']}/绑定/历史)")
    if apply:
        conn.execute(
            "UPDATE rbac_users SET name=?, display_name=?, updated_at=datetime('now') WHERE id=?",
            (work_id, new_display, uid),
        )
        conn.commit()


def _business_counts(conn, uid: int) -> dict:
    """统计某 uid(数字) 名下业务数据: messages/memories/usage_records/scheduled_tasks。"""
    tags = {uid, f"web:{uid}", f"dingtalk:{uid}", str(uid)}
    out = {}
    ph = ",".join("?" for _ in tags)
    args = list(tags)
    out["messages"] = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE user_id IN ({ph}) OR session_id LIKE 'web:{uid}:%' "
        f"OR session_id LIKE 'dingtalk:{uid}:%' OR session_id = ?", args + [f"web:{uid}"]).fetchone()[0]
    out["memories"] = conn.execute(
        f"SELECT COUNT(*) FROM memories WHERE owner_id IN ({ph})", args).fetchone()[0]
    out["usage_records"] = conn.execute(
        f"SELECT COUNT(*) FROM usage_records WHERE user_id IN ({ph})", args).fetchone()[0]
    out["scheduled_tasks"] = conn.execute(
        "SELECT COUNT(*) FROM scheduled_tasks WHERE user_id=?", (uid,)).fetchone()[0]
    return out


def delete_duplicate(conn, apply: bool, uid: int, work_id: str) -> None:
    """删除无独立业务数据的重复账号(uid34 类)。有数据则中止提示人工。"""
    row = conn.execute(
        "SELECT id, name, role, status FROM rbac_users WHERE id=?", (uid,)).fetchone()
    if not row:
        print(f"[migrate] uid={uid} 不存在, 跳过")
        return
    counts = _business_counts(conn, uid)
    if any(counts.values()):
        print(f"[migrate] 中止: uid={uid}(name={row['name']}) 名下仍有业务数据 {counts}, "
              f"请人工处理后再删(勿直接删除)")
        return
    print(f"[migrate] 删除重复账号 uid={uid}(name={row['name']}, role={row['role']}) "
          f"—— 名下无 messages/memories/usage/scheduled 数据")
    if apply:
        conn.execute("DELETE FROM rbac_user_identities WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM rbac_users WHERE id=?", (uid,))
        conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="SSO 双账号合并一次性迁移(默认 dry-run)")
    ap.add_argument("--db", default="", help="SQLite data.db 路径(默认自动定位)")
    ap.add_argument("--apply", action="store_true", help="真正落库; 缺省仅预览")
    ap.add_argument("--old-uid", type=int, default=1, help="老账号(中文名) uid, 默认 1")
    ap.add_argument("--dup-uid", type=int, default=34, help="重复账号(工号名) uid, 默认 34")
    ap.add_argument("--work-id", default="202202100024", help="SSO 工号(sub)")
    ap.add_argument("--zh-name", default="季明清", help="中文显示名(claims.name)")
    args = ap.parse_args()

    db = resolve_db(args.db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        ensure_display_name_col(conn, args.apply)
        merge_old_account(conn, args.apply, args.old_uid, args.work_id, args.zh_name)
        delete_duplicate(conn, args.apply, args.dup_uid, args.work_id)
    finally:
        conn.close()
    mode = "已落库(APPLY)" if args.apply else "DRY-RUN(预览, 未改动; 加 --apply 执行)"
    print(f"[migrate] 完成: {mode} | db={db}")


if __name__ == "__main__":
    main()
