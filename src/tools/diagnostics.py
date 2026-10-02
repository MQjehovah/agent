"""代码诊断工具 — 按项目类型运行 linter/类型检查，输出结构化问题列表。

- 探测工作区类型（pyproject/setup → Python；package.json → Node；go.mod → Go；Cargo.toml → Rust）
- 运行可用的检查器（ruff/mypy/eslint/tsc/go vet/cargo check），统一解析为结构化 issues
- 检查器缺失时标记 unavailable，不影响其它检查器
- `advisory_for_paths()` 供写工具后追加“仅提示”诊断（不阻断）
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


# ── 语言探测 ─────────────────────────────────────────

def detect_languages(workspace: str) -> list[str]:
    """根据工作区根目录标记文件探测语言（去重、稳定顺序）。"""
    langs: list[str] = []
    markers = [
        ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
        ("node", ("package.json", "tsconfig.json")),
        ("go", ("go.mod",)),
        ("rust", ("Cargo.toml",)),
    ]
    for lang, files in markers:
        if any(os.path.isfile(os.path.join(workspace, f)) for f in files):
            langs.append(lang)
    return langs


def _ext_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return "python"
    if ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"):
        return "node"
    if ext == ".go":
        return "go"
    if ext == ".rs":
        return "rust"
    return ""


# ── 解析器 ───────────────────────────────────────────

def _parse_ruff(payload: str) -> list[dict]:
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return []
    issues = []
    for item in data if isinstance(data, list) else []:
        loc = item.get("location") or {}
        code = str(item.get("code") or "")
        severity = "error" if code[:1] in ("E", "F") or code.startswith("W6") else "warning"
        issues.append({
            "file": item.get("filename", ""),
            "line": loc.get("row", 0),
            "col": loc.get("column", 0),
            "severity": severity,
            "code": code,
            "message": item.get("message", ""),
        })
    return issues


_MYPY_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>error|warning|note):\s*(?P<msg>.*)$")


def _parse_mypy(payload: str) -> list[dict]:
    issues = []
    for line in (payload or "").splitlines():
        m = _MYPY_RE.match(line.strip())
        if not m:
            continue
        issues.append({
            "file": m.group("file"),
            "line": int(m.group("line")),
            "col": int(m.group("col") or 0),
            "severity": "error" if m.group("sev") == "error" else "warning",
            "code": "",
            "message": m.group("msg"),
        })
    return issues


def _parse_eslint(payload: str) -> list[dict]:
    try:
        data = json.loads(payload or "[]")
    except json.JSONDecodeError:
        return []
    issues = []
    for entry in data if isinstance(data, list) else []:
        fname = entry.get("filePath", "")
        for msg in entry.get("messages", []):
            issues.append({
                "file": fname,
                "line": msg.get("line", 0) or 0,
                "col": msg.get("column", 0) or 0,
                "severity": "error" if msg.get("severity") == 2 else "warning",
                "code": str(msg.get("ruleId") or ""),
                "message": msg.get("message", ""),
            })
    return issues


_TSC_RE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<sev>error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>.*)$")


def _parse_tsc(payload: str) -> list[dict]:
    issues = []
    for line in (payload or "").splitlines():
        m = _TSC_RE.match(line.strip())
        if not m:
            continue
        issues.append({
            "file": m.group("file"),
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "severity": "error" if m.group("sev") == "error" else "warning",
            "code": m.group("code"),
            "message": m.group("msg"),
        })
    return issues


def _parse_generic(payload: str) -> list[dict]:
    """兜底：把非空输出行作为 issue（file/line 未知）。"""
    issues = []
    for line in (payload or "").splitlines():
        text = line.strip()
        if not text:
            continue
        issues.append({"file": "", "line": 0, "col": 0, "severity": "warning", "code": "", "message": text})
        if len(issues) >= 50:
            break
    return issues


# ── 检查器规格 ───────────────────────────────────────

def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _node_bin(workspace: str, name: str) -> str | None:
    """返回项目内 node_modules/.bin 下的可执行文件路径。"""
    for cand in (name, f"{name}.cmd", f"{name}.exe"):
        p = os.path.join(workspace, "node_modules", ".bin", cand)
        if os.path.isfile(p):
            return p
    return None


def _runner_specs(workspace: str, language: str, target: str) -> list[dict]:
    specs: list[dict] = []
    if language == "python":
        if _has_module("ruff"):
            specs.append({
                "tool": "ruff",
                "cmd": [sys.executable, "-m", "ruff", "check", "--output-format=json", target],
                "parse": _parse_ruff, "json": True,
            })
        if _has_module("mypy"):
            specs.append({
                "tool": "mypy",
                "cmd": [sys.executable, "-m", "mypy", "--no-error-summary", "--show-column-numbers", target],
                "parse": _parse_mypy, "json": False,
            })
    elif language == "node":
        eslint = _node_bin(workspace, "eslint")
        if eslint:
            specs.append({
                "tool": "eslint",
                "cmd": [eslint, "-f", "json", target],
                "parse": _parse_eslint, "json": True,
            })
        tsc = _node_bin(workspace, "tsc")
        if tsc:
            specs.append({
                "tool": "tsc",
                "cmd": [tsc, "--noEmit", "--pretty", "false"],
                "parse": _parse_tsc, "json": False,
            })
    elif language == "go" and shutil.which("go"):
        specs.append({
            "tool": "go vet",
            "cmd": ["go", "vet", "./..."],
            "parse": _parse_generic, "json": False,
        })
    elif language == "rust" and shutil.which("cargo"):
        specs.append({
            "tool": "cargo check",
            "cmd": ["cargo", "check", "--message-format=short"],
            "parse": _parse_generic, "json": False,
        })
    return specs


def _run(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """运行命令，返回 (returncode, stdout, stderr)；超时/缺失返回 (-1, '', 原因)。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout,
            text=True, encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as e:
        return -1, "", f"命令不可用: {e}"
    except subprocess.TimeoutExpired:
        return -1, "", f"超时({timeout}s)"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def run_diagnostics(workspace: str, paths: list[str] | None = None,
                    languages: list[str] | None = None, timeout: int = 60) -> dict:
    """运行诊断，返回结构化结果。"""
    workspace = workspace or os.getcwd()
    if languages:
        langs = [lang for lang in languages if lang]
    else:
        ext_langs = {_ext_language(p) for p in (paths or [])}
        ext_langs.discard("")
        langs = sorted(ext_langs) if ext_langs else detect_languages(workspace)

    target = " ".join(paths) if len(paths or []) == 1 else "."
    results = []
    for lang in langs:
        specs = _runner_specs(workspace, lang, target)
        if not specs:
            results.append({"tool": lang, "status": "unavailable",
                            "issue_count": 0, "issues": [],
                            "reason": "未找到可用检查器"})
            continue
        for spec in specs:
            code, out, err = _run(spec["cmd"], workspace, timeout)
            if code == -1:
                results.append({"tool": spec["tool"], "status": "unavailable",
                                "issue_count": 0, "issues": [], "reason": err})
                continue
            issues = spec["parse"](out)
            results.append({
                "tool": spec["tool"],
                "status": "issues" if issues else "ok",
                "issue_count": len(issues),
                "issues": issues,
                "raw_tail": (err or out)[-500:] if (err and not issues) else "",
            })
    return {"workspace": workspace, "languages": langs, "results": results}


def format_advisory(result: dict, max_issues: int = 10) -> str:
    """把诊断结果压成给 LLM 看的简短“仅提示”文本；无问题返回空串。"""
    lines: list[str] = []
    shown = 0
    for res in result.get("results", []):
        if res.get("status") != "issues":
            continue
        if not lines:
            lines.append("代码诊断（仅供参考，未阻断）:")
        lines.append(f"- {res['tool']}: {res['issue_count']} 个问题")
        for issue in res.get("issues", []):
            if shown >= max_issues:
                lines.append("  …（更多问题已省略）")
                break
            loc = f"{issue.get('file','?')}:{issue.get('line',0)}:{issue.get('col',0)}"
            code = f" {issue['code']}" if issue.get("code") else ""
            lines.append(f"  {loc} {issue.get('severity','warning')}{code} {issue.get('message','')}")
            shown += 1
    return "\n".join(lines)


async def advisory_for_paths(workspace: str, paths: list[str], timeout: int = 20,
                             max_issues: int = 10) -> str:
    """写工具后追加诊断（仅提示）。任何异常/不可用都返回空串。"""
    if not paths:
        return ""
    try:
        result = await asyncio.to_thread(run_diagnostics, workspace, paths, None, timeout)
        return format_advisory(result, max_issues)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[diagnostics] 建议诊断跳过: {e}")
        return ""


class CodeDiagnosticsTool(BuiltinTool):
    """代码诊断工具（linter / 类型检查）"""

    @property
    def name(self) -> str:
        return "code_diagnostics"

    @property
    def description(self) -> str:
        return """运行项目代码诊断（linter 与类型检查），返回结构化问题列表。

自动探测语言并选择可用检查器：
- Python: ruff（lint）、mypy（类型）
- Node: eslint、tsc
- Go: go vet
- Rust: cargo check

用法:
- 全项目: {"path": "."} 或省略 path
- 指定文件/目录: {"path": "src/main.py"}
- 限定语言: {"path": ".", "languages": ["python"]}

返回每条问题含 file/line/col/severity/code/message。检查器未安装会标记 unavailable。"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要检查的文件或目录（相对工作区，默认整个工作区）"
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定语言（可选）：python/node/go/rust"
                },
                "timeout": {
                    "type": "integer",
                    "description": "单个检查器超时秒数（默认 60）",
                    "default": 60
                },
            },
        }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        languages = kwargs.get("languages") or None
        timeout = int(kwargs.get("timeout", 60) or 60)

        paths = None
        if path:
            full = self.resolve_path(path)
            if not self.is_path_allowed(full) and not os.path.isdir(full):
                # 诊断只读：允许工作区外只读目录，但默认限定工作区
                pass
            rel = os.path.relpath(full, self.workspace or os.getcwd())
            paths = [rel]

        try:
            result = await asyncio.to_thread(
                run_diagnostics, self.workspace or os.getcwd(), paths, languages, timeout
            )
        except Exception as e:  # noqa: BLE001
            return json.dumps({"success": False, "error": f"诊断执行失败: {e}"}, ensure_ascii=False)

        result["success"] = True
        return json.dumps(result, ensure_ascii=False, indent=2)
