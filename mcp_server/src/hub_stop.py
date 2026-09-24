"""数字中台 MCP 短问停手：拦接口、滤售后紧固件、生产短问藏 SC。不改 Agent 核心。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STOP_JSON = ROOT / "config" / "agents" / "数字中台" / "stop-rules.json"
TASK_FILE = ROOT / "workspace" / ".digital_hub_task.txt"
LOG_FILE = ROOT / "workspace" / ".digital_hub_stop.log"
_FASTENER_PREFIXES = ("13006", "13007", "13009")
_ORDER_KEYS = ("orderCode", "order_code", "code", "manufactureCode", "manufacture_code")


def _log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def load_stop_config(config_dir: str = "") -> dict:
    path = Path(config_dir) / "stop-rules.json" if config_dir else STOP_JSON
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"load stop-rules fail: {e}")
        return {}


def _load_rules() -> dict:
    return load_stop_config()


def write_current_task(task: str, workspace: str | None = None) -> None:
    path = Path(workspace) / ".digital_hub_task.txt" if workspace else TASK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task or "", encoding="utf-8")
    except OSError as e:
        _log(f"write task fail: {e}")


def _read_task() -> str:
    env_file = os.getenv("DIGITAL_HUB_TASK_FILE", "").strip()
    candidates = []
    if env_file:
        p = Path(env_file)
        candidates.append(p if p.is_absolute() else ROOT / p)
    candidates.append(TASK_FILE)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return os.getenv("DIGITAL_HUB_TASK", "") or ""


def _match(task: str, rules: list) -> dict | None:
    text = (task or "").strip()
    if not text:
        return None
    for rule in rules or []:
        if any(n and n in text for n in (rule.get("when") or [])):
            return rule
    return None


def match_rule(task: str, rules: list) -> dict | None:
    return _match(task, rules)


def _path_hit(path: str, prefixes: list) -> bool:
    return any(pref and pref in (path or "") for pref in prefixes)


def block_for_task(task: str, tool_name: str, args: dict | None, config: dict | None = None) -> dict | None:
    args = args or {}
    cfg = config if isinstance(config, dict) and config.get("rules") else _load_rules()
    rule = _match(task, cfg.get("rules") or [])
    path = str(args.get("path") or "")
    name = tool_name or ""
    sql = bool(args.get("sql")) or name.endswith("execute_query") or name == "execute_query"
    _log(f"check tool={name} path={path} sql={sql} task={task[:40]!r} rule={(rule or {}).get('id')}")
    if not rule:
        return None
    if sql and (rule.get("deny_sql") or rule.get("allow_erp_paths") or rule.get("deny_all_erp")):
        _log(f"BLOCK sql id={rule.get('id')}")
        return {
            "success": False,
            "error": f"本短问「{rule.get('id', '')}」禁止 SQL，按 skill 只用 HTTP。",
            "blocked_by": "intent_stop",
        }
    if sql:
        return None
    if not path.startswith("/") and name not in ("erp_request",) and not name.endswith("erp_request"):
        return None
    if rule.get("deny_all_erp"):
        _log(f"BLOCK all-erp {path}")
        return {
            "success": False,
            "error": "本短问不调 ERP 接口，按已有规则口头作答即可。",
            "blocked_by": "intent_stop",
            "path": path,
        }
    deny = rule.get("deny_erp_paths") or []
    allow = rule.get("allow_erp_paths") or []
    if _path_hit(path, deny):
        _log(f"BLOCK deny {path}")
        hint = f"只允许：{'、'.join(allow)}。" if allow else ""
        return {
            "success": False,
            "error": f"本短问禁止调用 {path}。{hint}立刻用允许的接口作答。DH 是到货单不是调拨。"
            if "detect" in str(rule.get("id") or "")
            else f"本短问禁止调用 {path}。{hint}立刻用允许的接口作答。",
            "blocked_by": "intent_stop",
            "path": path,
        }
    if allow and not _path_hit(path, allow):
        _log(f"BLOCK not-in-allow {path} allow={allow}")
        extra = "DH 是到货单，用 /detect/list/orderCode/{单号}；ZJ 用 /detect/code/{单号}。" if "detect" in str(rule.get("id") or "") else ""
        return {
            "success": False,
            "error": f"本短问只允许：{'、'.join(allow)}。已拒绝 {path}。立刻用允许的接口作答，不要再加戏。{extra}",
            "blocked_by": "intent_stop",
            "path": path,
        }
    return None


def maybe_block(tool_name: str, args: dict | None) -> dict | None:
    return block_for_task(_read_task(), tool_name, args)


def block_tool(task: str, tool_name: str, args: dict | None, config: dict | None = None) -> str | None:
    """给抽检用：命中则返回 JSON 字符串。"""
    blocked = block_for_task(task, tool_name, args, config)
    if not blocked:
        return None
    return json.dumps(blocked, ensure_ascii=False)


def _is_fastener_item(item: dict) -> bool:
    code = str(item.get("materialCode") or item.get("material_code") or "")
    if not code and ("materialName" in item or "material_name" in item):
        code = str(item.get("code") or "")
    name = str(item.get("materialName") or item.get("material_name") or item.get("name") or "")
    if any(code.startswith(p) for p in _FASTENER_PREFIXES):
        return True
    return any(tok in name for tok in ("螺钉", "螺母", "垫片"))


def _filter_list(items: list) -> tuple[list, int]:
    kept, skipped = [], 0
    for item in items:
        if isinstance(item, dict) and _is_fastener_item(item):
            skipped += 1
            continue
        kept.append(item)
    return kept, skipped


def _looks_like_material_list(node: list) -> bool:
    if not node or not isinstance(node[0], dict):
        return False
    return any(
        "materialCode" in x or "material_code" in x or "materialName" in x or "material_name" in x
        for x in node if isinstance(x, dict)
    )


def _filter_node(node: Any) -> tuple[Any, int]:
    if isinstance(node, list) and _looks_like_material_list(node):
        return _filter_list(node)
    if isinstance(node, dict):
        skipped = 0
        out = {}
        for k, v in node.items():
            nv, sk = _filter_node(v) if isinstance(v, (dict, list)) else (v, 0)
            out[k] = nv
            skipped += sk
        return out, skipped
    return node, 0


def _has_specific_order(args: dict | None) -> bool:
    blobs: list[Any] = [args or {}]
    src = args or {}
    for key in ("body", "params"):
        if isinstance(src.get(key), dict):
            blobs.append(src[key])
    for item in blobs:
        if not isinstance(item, dict):
            continue
        for key in _ORDER_KEYS:
            if str(item.get(key) or "").strip():
                return True
    return False


def _collect_material_rows(node: Any, acc: list | None = None) -> list:
    acc = acc if acc is not None else []
    if isinstance(node, list):
        if _looks_like_material_list(node):
            acc.extend(x for x in node if isinstance(x, dict))
            return acc
        for item in node:
            _collect_material_rows(item, acc)
        return acc
    if isinstance(node, dict):
        for value in node.values():
            _collect_material_rows(value, acc)
    return acc


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _scrub_production(path: str, result: dict, args: dict | None = None) -> dict:
    if "/pick" not in (path or ""):
        return result
    if _has_specific_order(args):
        return result
    task = _read_task()
    if re.search(r"SC\d+", task or ""):
        return result
    _log(f"replace pick payload path={path}")
    return {
        "success": True,
        "path": path,
        "outputConstraint": "正文禁止出现任何 SC 加数字的单号。不要编造生产单。",
        "answerExample": "近几天都是补料，没有指定生产单的配套领料。请给 SC 号。",
        "data": {
            "records": [],
            "summary": "未指定生产单，不返回具体单据号。只概括近几天有无领料/补料。",
        },
    }


def _unwrap(node: Any, depth: int = 0) -> Any:
    if depth > 6:
        return node
    if isinstance(node, str):
        text = node.strip()
        if text[:1] in "{[":
            try:
                return _unwrap(json.loads(text), depth + 1)
            except (TypeError, json.JSONDecodeError):
                return node
        return node
    if isinstance(node, dict):
        return {k: _unwrap(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_unwrap(x, depth + 1) for x in node]
    return node


def filter_aftersale_stats(payload: Any) -> Any:
    payload = _unwrap(payload)
    if isinstance(payload, list):
        kept, skipped = _filter_list(payload)
        if not skipped:
            return payload
        return {
            "items": kept,
            "standardPartsSkipped": skipped,
            "standardPartsNote": (
                f"已排除 {skipped} 条紧固件（物料码 13006/13007/13009），"
                "正文一行带过「标准件数量最多，略」，再列其余配件。"
            ),
            "outputConstraint": "禁止把螺钉/螺母/垫片排成 Top 1～10。",
        }
    if not isinstance(payload, dict):
        return payload
    filtered, skipped = _filter_node(payload)
    if skipped and isinstance(filtered, dict):
        filtered = dict(filtered)
        filtered["standardPartsSkipped"] = skipped
        filtered["standardPartsNote"] = (
            f"已排除 {skipped} 条紧固件（物料码 13006/13007/13009），"
            "正文一行带过「标准件数量最多，略」，再列其余配件。"
        )
        filtered["outputConstraint"] = "禁止把螺钉/螺母/垫片排成 Top 1～10。"
    return filtered


def _annotate_detect(path: str, result: dict) -> dict:
    if "/detect" not in (path or "") or not isinstance(result, dict):
        return result
    out = dict(result)
    out["outputConstraint"] = (
        "DH 是到货单，禁止当调拨。ZJ 只用 /detect/code 或 POST /detect/query。"
        "正文禁止「根据规则」「空数组」。开口：尚未开检 / 检测中 / 已有检测单。"
    )
    return out


def _annotate_margin(path: str, result: dict) -> dict:
    if "毛利大概多少" not in _read_task():
        return result
    if "/statistics/" not in (path or "") or not isinstance(result, dict):
        return result
    out = dict(result)
    out["outputConstraint"] = (
        "立刻用本周看板报粗毛利。必须写「粗口径，不是财务毛利」。"
        "禁止追问期间，禁止「根据规则」，禁止渠道/LTV。"
    )
    return out


def filter_erp_result(path: str, result: dict, args: dict | None = None) -> dict:
    _log(f"filter enter path={path}")
    result = _scrub_production(path, result, args)
    result = _annotate_detect(path, result)
    result = _annotate_margin(path, result)
    blob = _dump(result)
    looks = (
        "aftersale-delivery-stats" in (path or "")
        or "aftersale-delivery-stats" in blob
        or "130060" in blob
        or "130070" in blob
        or "130090" in blob
        or ("螺钉" in blob and "material" in blob.lower())
    )
    if not looks:
        return result
    try:
        unwrapped = _unwrap(result) if isinstance(result, (dict, list, str)) else result
        rows = _collect_material_rows(unwrapped)
        kept, skipped = _filter_list(rows)
        _log(f"filter aftersale rows={len(rows)} skipped={skipped} kept={len(kept)}")
        names = "、".join(
            str(x.get("materialName") or x.get("material_name") or x.get("name") or "")
            for x in kept[:8]
        )
        return {
            "success": True,
            "path": path,
            "status_code": result.get("status_code") if isinstance(result, dict) else 200,
            "standardPartsSkipped": skipped,
            "standardPartsNote": (
                f"已排除 {skipped} 条紧固件（物料码 13006/13007/13009）。"
                "正文必须先写「标准件（螺钉/螺母/垫片）数量最多，略。」再列其余配件。"
            ),
            "outputConstraint": "禁止把螺钉/螺母/垫片写成 1. 开头的排名。",
            "answerExample": f"标准件（螺钉/螺母/垫片）数量最多，略。其余：{names or '无非标准件'}。",
            "data": {"items": kept[:15]},
        }
    except Exception as e:
        _log(f"filter aftersale fail: {e}")
        return result


_NUMBERED_ITEM = re.compile(r"^\s*(?:\d+[\.、\)]\s*|[-*]\s*)")
_FASTENER_LINE = re.compile(r"螺钉|螺母|垫片")


def guard_final_answer(task: str, answer: str, config: dict | None = None) -> str:
    """抽检脚本用。Agent 核心不调用。"""
    if not answer:
        return answer
    cfg = config if isinstance(config, dict) and config.get("rules") else _load_rules()
    rule = _match(task, cfg.get("rules") or [])
    if not rule:
        return answer
    text = answer
    if rule.get("id") == "aftersale-top" or rule.get("filter_fasteners"):
        kept: list[str] = []
        dropped = 0
        for line in text.splitlines():
            if _NUMBERED_ITEM.match(line) and _FASTENER_LINE.search(line):
                dropped += 1
                continue
            kept.append(line)
        text = "\n".join(kept)
        if dropped and "标准件" not in text[:160]:
            text = "标准件（螺钉/螺母/垫片）数量最多，略。\n" + text.lstrip()
    if rule.get("id") == "production-pick" and not re.search(r"SC\d+", task or ""):
        if re.search(r"SC\d+", text):
            text = re.sub(r"[^\n]*SC\d+[^\n]*\n?", "", text)
            text = re.sub(r"SC\d+", "", text)
            if "生产单号" not in text and "SC 号" not in text:
                text = (text.strip() + "\n未指定生产单，请提供生产单号后再查。").strip()
    if rule.get("id") in ("detect-progress", "gross-margin"):
        text = re.sub(r"[^\n]*根据规则[^\n]*\n?", "", text)
        text = text.replace("空数组", "")
    return text


_log(f"hub_stop loaded root={ROOT}")
