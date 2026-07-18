"""
会话级撤销系统 — /undo 命令

设计思路（参考 grok-build 的 checkpoint + /undo）：
- 在每次文件修改前自动拍快照（git blob 或文件备份）
- /undo 按时间倒序恢复最近 N 步操作
- 支持 --code（只撤销代码修改）和 --conversation（只清 LLM 回复）
- 快照自动清理（1 小时后删除）

用法:
    undo = UndoManager(workspace)

    # 在文件操作前调用
    await undo.snapshot(["src/main.py", "src/utils.py"])

    # 撤销最近 1 步
    result = await undo.undo(steps=1, mode="code")

    # 查看可撤销的操作
    history = undo.get_history()
"""
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.undo")


@dataclass
class Snapshot:
    """文件快照"""
    id: str
    timestamp: float
    files: dict[str, str]  # file_path -> backup_path
    description: str = ""
    tool_name: str = ""


class UndoManager:
    """撤销管理器 — 文件级快照 + 会话级恢复"""

    SNAPSHOT_DIR = ".agent/snapshots"
    SNAPSHOT_TTL = 3600  # 1 小时后自动清理
    MAX_SNAPSHOTS = 50

    def __init__(self, workspace: str = ""):
        self.workspace = workspace
        self._snapshot_dir = os.path.join(workspace, self.SNAPSHOT_DIR) if workspace else ""
        self._snapshots: list[Snapshot] = []
        self._conversation_history: list[dict] = []  # 对话快照
        self._load_snapshots()

    # ── 核心接口 ──

    async def snapshot_before_edit(
        self,
        file_paths: list[str],
        tool_name: str = "",
        description: str = "",
    ) -> Optional[str]:
        """在文件修改前创建快照

        Args:
            file_paths: 要快照的文件列表
            tool_name: 工具名（如 edit, batch_edit）
            description: 描述

        Returns:
            快照 ID，如果所有文件都不存在则返回 None
        """
        if not file_paths:
            return None

        os.makedirs(self._snapshot_dir, exist_ok=True)

        snapshot_id = str(int(time.time() * 1000))
        backup_dir = os.path.join(self._snapshot_dir, snapshot_id)
        os.makedirs(backup_dir, exist_ok=True)

        files_backup: dict[str, str] = {}
        for fpath in file_paths:
            full = fpath if os.path.isabs(fpath) else os.path.join(self.workspace, fpath)
            if os.path.isfile(full):
                # 生成备份文件名（用哈希避免路径冲突）
                safe_name = hashlib.md5(full.encode()).hexdigest()[:12] + "_" + os.path.basename(full)
                backup_path = os.path.join(backup_dir, safe_name)
                try:
                    shutil.copy2(full, backup_path)
                    files_backup[fpath] = backup_path
                except Exception as e:
                    logger.warning(f"[undo] 备份失败 {fpath}: {e}")

        if not files_backup:
            # 没有备份任何文件，清理空目录
            try:
                os.rmdir(backup_dir)
            except Exception:
                pass
            return None

        snapshot = Snapshot(
            id=snapshot_id,
            timestamp=time.time(),
            files=files_backup,
            tool_name=tool_name,
            description=description,
        )
        self._snapshots.append(snapshot)
        self._trim_excess()
        self._save_index()

        logger.info(f"[undo] 📸 快照 {snapshot_id[:8]}: {len(files_backup)} 个文件 ({tool_name})")
        return snapshot_id

    async def snapshot_conversation(self, messages: list[dict]) -> str:
        """保存对话快照（用于 /undo --conversation）"""
        snapshot_id = str(int(time.time() * 1000))
        self._conversation_history.append({
            "id": snapshot_id,
            "timestamp": time.time(),
            "messages": list(messages),
        })
        # 只保留最近 20 条对话快照
        if len(self._conversation_history) > 20:
            self._conversation_history = self._conversation_history[-20:]
        return snapshot_id

    async def undo(
        self,
        steps: int = 1,
        mode: str = "code",
    ) -> dict:
        """撤销操作

        Args:
            steps: 撤销几步（默认 1）
            mode: "code" 只恢复文件 / "conversation" 只恢复对话 / "both"

        Returns:
            {"success": true, "files_restored": [...], "conversation_restored": bool}
        """
        result = {"success": True, "files_restored": [], "conversation_restored": False}

        if mode in ("code", "both"):
            files_restored = self._restore_files(steps)
            result["files_restored"] = files_restored

        if mode in ("conversation", "both"):
            conv_restored = self._restore_conversation(steps)
            result["conversation_restored"] = conv_restored

        logger.info(f"[undo] ↩️ 撤销 {steps} 步 ({mode}): {len(result['files_restored'])} 个文件恢复")
        return result

    def get_history(self, limit: int = 20) -> list[dict]:
        """获取可撤销的操作历史"""
        history = []
        for s in reversed(self._snapshots[-limit:]):
            history.append({
                "id": s.id[:8],
                "time_ago": f"{int((time.time() - s.timestamp) // 60)} 分钟前",
                "tool": s.tool_name or "未知",
                "files": list(s.files.keys()),
                "description": s.description,
            })
        return history

    def get_conversation_history(self, limit: int = 10) -> list[dict]:
        return [
            {
                "id": c["id"][:8],
                "time_ago": f"{int((time.time() - c['timestamp']) // 60)} 分钟前",
                "messages": len(c["messages"]),
            }
            for c in reversed(self._conversation_history[-limit:])
        ]

    def get_stats(self) -> dict:
        return {
            "snapshots": len(self._snapshots),
            "conversation_snapshots": len(self._conversation_history),
            "snapshot_dir": self._snapshot_dir,
            "oldest_snapshot_ago_s": int(time.time() - self._snapshots[0].timestamp) if self._snapshots else None,
            "newest_snapshot_ago_s": int(time.time() - self._snapshots[-1].timestamp) if self._snapshots else None,
        }

    # ── 内部 ──

    def _restore_files(self, steps: int) -> list[str]:
        """恢复文件快照"""
        if not self._snapshots:
            return []

        restored = []
        for _ in range(min(steps, len(self._snapshots))):
            snapshot = self._snapshots.pop()
            for rel_path, backup_path in snapshot.files.items():
                target = rel_path if os.path.isabs(rel_path) else os.path.join(self.workspace, rel_path)
                try:
                    if os.path.isfile(backup_path):
                        shutil.copy2(backup_path, target)
                        restored.append(rel_path)
                        logger.info(f"[undo] 恢复 {rel_path}")
                except Exception as e:
                    logger.warning(f"[undo] 恢复失败 {rel_path}: {e}")

        self._save_index()
        return restored

    def _restore_conversation(self, steps: int) -> bool:
        """恢复对话（移除最近的 N 条用户消息）"""
        if not self._conversation_history:
            return False
        for _ in range(min(steps, len(self._conversation_history))):
            self._conversation_history.pop()
        return True

    def _trim_excess(self):
        """保持快照数量在限制内"""
        while len(self._snapshots) > self.MAX_SNAPSHOTS:
            old = self._snapshots.pop(0)
            self._cleanup_snapshot_files(old)

    def _cleanup_snapshot_files(self, snapshot: Snapshot):
        """清理快照文件"""
        backup_dir = os.path.dirname(next(iter(snapshot.files.values()))) if snapshot.files else ""
        if backup_dir and os.path.isdir(backup_dir):
            try:
                shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception:
                pass

    # ── 持久化 ──

    INDEX_FILE = "undo_index.json"

    def _save_index(self):
        """保存快照索引"""
        if not self._snapshot_dir:
            return
        try:
            os.makedirs(self._snapshot_dir, exist_ok=True)
            index_path = os.path.join(self._snapshot_dir, self.INDEX_FILE)
            data = []
            for s in self._snapshots:
                data.append({
                    "id": s.id,
                    "timestamp": s.timestamp,
                    "files": s.files,
                    "tool_name": s.tool_name,
                    "description": s.description,
                })
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"[undo] 保存索引失败: {e}")

    def _load_snapshots(self):
        """加载快照索引"""
        if not self._snapshot_dir:
            return
        index_path = os.path.join(self._snapshot_dir, self.INDEX_FILE)
        if not os.path.isfile(index_path):
            return
        try:
            with open(index_path, encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                self._snapshots.append(Snapshot(
                    id=d["id"],
                    timestamp=d["timestamp"],
                    files=d.get("files", {}),
                    tool_name=d.get("tool_name", ""),
                    description=d.get("description", ""),
                ))
            logger.info(f"[undo] 加载了 {len(self._snapshots)} 个快照")
        except Exception as e:
            logger.warning(f"[undo] 加载索引失败: {e}")

    # ── 自动清理 ──

    async def cleanup_expired(self):
        """清理过期快照"""
        now = time.time()
        expired = [s for s in self._snapshots if now - s.timestamp > self.SNAPSHOT_TTL]
        for s in expired:
            self._snapshots.remove(s)
            self._cleanup_snapshot_files(s)
        if expired:
            self._save_index()
            logger.info(f"[undo] 清理了 {len(expired)} 个过期快照")


# ── 集成辅助 ──

def undo_tool(undo_manager):
    """创建 /undo 工具定义（供 ToolRegistry 注册）"""
    return {
        "name": "undo",
        "description": "撤销最近的文件修改操作。支持 --code 撤销代码改动，--conversation 回退对话。",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {"type": "integer", "description": "撤销几步（默认 1）"},
                "mode": {
                    "type": "string",
                    "enum": ["code", "conversation", "both"],
                    "description": "撤销模式",
                },
            },
        },
    }

async def execute_undo(undo_manager, steps: int = 1, mode: str = "code") -> str:
    """执行撤销"""
    result = await undo_manager.undo(steps=steps, mode=mode)
    return json.dumps(result, ensure_ascii=False)
