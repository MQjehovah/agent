"""附件存储：图片落盘为对象（按 sha256 去重），消息里只存引用，不存字节。

- 引用形态：`store:<sha256>.<ext>`（本模块管理的对象）或普通文件路径（desktop 附件）。
- 存储位置：<workspace>/.attachments/store/<sha256>.<ext>。
- 预留 MinIO：设置 MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET 且安装了 minio 包时优先用对象存储，
  否则回退本地文件（同上述路径）。当前默认本地存储（单机足够、零新依赖）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os

logger = logging.getLogger("agent.attachments")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def mime_for(name_or_ref: str) -> str:
    ext = os.path.splitext(str(name_or_ref).split("?", 1)[0])[1].lower()
    return _MIME.get(ext, "image/png")


def store_dir(workspace: str) -> str:
    return os.path.join(workspace or ".", ".attachments", "store")


def _minio():
    """可选对象存储后端（未配置/未安装则返回 None）。"""
    endpoint = os.environ.get("MINIO_ENDPOINT", "").strip()
    if not endpoint:
        return None
    try:
        from minio import Minio
    except ImportError:
        logger.debug("未安装 minio 包，附件使用本地存储")
        return None
    try:
        client = Minio(
            endpoint,
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            secure=os.environ.get("MINIO_SECURE", "0").lower() in ("1", "true", "yes"),
        )
        return client, os.environ.get("MINIO_BUCKET", "xzrobotserver")
    except Exception as exc:  # noqa: BLE001
        logger.warning("MinIO 初始化失败，附件回退本地存储: %s", exc)
        return None


def save_bytes(data: bytes, name: str, workspace: str = "") -> dict:
    """保存图片字节，返回 {ref, name, size, url}。按内容 sha256 去重。"""
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(name or "")[1].lower() or ".png"
    ref = f"store:{sha}{ext}"
    cli = _minio()
    if cli is not None:
        client, bucket = cli
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            key = f"{sha}{ext}"
            client.put_object(bucket, key, io.BytesIO(data), length=len(data),
                              content_type=mime_for(ext))
            endpoint = os.environ.get("MINIO_ENDPOINT", "")
            scheme = "https" if os.environ.get("MINIO_SECURE", "0").lower() in ("1", "true") else "http"
            return {"ref": ref, "name": name or ref, "size": len(data),
                    "url": f"{scheme}://{endpoint}/{bucket}/{key}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("MinIO 上传失败，附件回退本地存储: %s", exc)
    d = store_dir(workspace)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sha}{ext}")
    if not os.path.isfile(path):
        with open(path, "wb") as f:
            f.write(data)
    return {"ref": ref, "name": name or ref, "size": len(data), "url": ""}


def _store_id_to_path(ref: str, workspace: str) -> str:
    key = ref.split("store:", 1)[1]
    return os.path.join(store_dir(workspace), key)


def load_bytes(ref: str, workspace: str = "") -> bytes | None:
    """按引用读取图片字节：支持 'store:<id>' 与普通文件路径（相对 workspace 解析）。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("store:"):
        key = ref.split("store:", 1)[1]
        cli = _minio()
        if cli is not None:
            client, bucket = cli
            try:
                resp = client.get_object(bucket, key)
                try:
                    return resp.read()
                finally:
                    resp.close()
                    resp.release_conn()
            except Exception:  # noqa: BLE001
                pass
        p = _store_id_to_path(ref, workspace)
        if os.path.isfile(p):
            return _read_file(p)
        return None
    # 普通路径（含 desktop 的 .attachments/xxx.png）
    for cand in ([ref] if os.path.isabs(ref) else [os.path.join(workspace or ".", ref), ref]):
        if os.path.isfile(cand):
            return _read_file(cand)
    return None


def _read_file(path: str, max_bytes: int = 10 * 1024 * 1024) -> bytes | None:
    try:
        if os.path.getsize(path) > max_bytes:
            logger.warning("图片超过上限，忽略: %s", path)
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        logger.warning("读取图片失败: %s (%s)", path, exc)
        return None


def to_data_url(ref: str, workspace: str = "") -> str | None:
    data = load_bytes(ref, workspace)
    if not data:
        return None
    return f"data:{mime_for(ref)};base64," + base64.b64encode(data).decode("ascii")
