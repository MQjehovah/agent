"""W3b 新增 MCP servers(mcp_git/mcp_postgres) 进程内测试。

- git: 本机 git CLI + tmp_path 真仓库(init/config/commit); 覆盖只读工具输出、
  白名单越界/非仓库/子目录/未配置拒绝、写开关默认关闭、选项注入与路径逃逸拒绝、
  输出截断、空提交信息与 git 非零退出映射、裸仓库只读例外; 测试内 git 命令同样 shell=False;
- postgres: 不真连 DB; 覆盖 is_read_only_sql 注入式绕过(多语句/数据修改型 CTE/
  注释与字符串掩码/未闭合)、clamp_limit 与行数上限、DSN 缺失文案、连接失败错误映射、
  假连接注入下的 pg_query 列/行/截断行为与连接级只读参数。
"""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))

from mcp import Client  # noqa: E402


def _load(name):
    return importlib.import_module(name)


def _payload(result):
    """把 CallToolResult 还原为 dict/str: 优先 structured_content, 否则解析文本 JSON。"""
    assert result.is_error is not True
    if result.structured_content is not None:
        return result.structured_content
    text = "\n".join(getattr(item, "text", "") or "" for item in (result.content or []))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _text(data):
    """兼容 bytes/str 两种 subprocess 输出(个别发行版把 Popen 默认设为文本模式)。"""
    if isinstance(data, str):
        return data
    return (data or b"").decode("utf-8", errors="replace")


def _git(cwd, *args):
    """测试用 git 调用(shell=False); 失败直接断言, 返回解码后的 stdout。"""
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, shell=False)
    assert result.returncode == 0, _text(result.stderr)
    return _text(result.stdout)


# ---------------------------------------------------------------- git

@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """白名单根目录 = tmp_path/roots, 内含 repo 仓库(两次提交)与 not_repo 普通目录。"""
    roots = tmp_path / "roots"
    roots.mkdir()
    repo = roots / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    (repo / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")
    _git(repo, "add", "--", "hello.txt", "notes.txt")
    _git(repo, "commit", "-m", "initial commit")
    (repo / "hello.txt").write_text("hello world\n", encoding="utf-8")
    _git(repo, "add", "--", "hello.txt")
    _git(repo, "commit", "-m", "second commit")
    (roots / "not_repo").mkdir()
    monkeypatch.setenv("GIT_MCP_ROOTS", str(roots))
    monkeypatch.delenv("GIT_MCP_ALLOW_WRITE", raising=False)
    monkeypatch.delenv("GIT_MCP_TIMEOUT", raising=False)
    monkeypatch.delenv("GIT_MCP_MAX_OUTPUT_CHARS", raising=False)
    return _load("mcp_git"), roots, repo


def test_git_read_tools_real_repo(git_env):
    module, roots, repo = git_env
    (repo / "hello.txt").write_text("hello again\n", encoding="utf-8")

    status = module.git_status(str(repo))
    assert status["ok"] is True
    assert "hello.txt" in status["output"]

    log = module.git_log(str(repo), limit=1)
    assert log["ok"] is True and log["limit"] == 1
    assert "second commit" in log["output"]
    assert "initial commit" not in log["output"]
    assert module.git_log(str(repo), limit=9999)["limit"] == 200

    file_log = module.git_log(str(repo), file="notes.txt")
    assert file_log["ok"] is True
    assert "initial commit" in file_log["output"]

    diff = module.git_diff(str(repo), ref="HEAD")
    assert diff["ok"] is True
    assert "hello again" in diff["output"]

    show = module.git_show(str(repo), ref="HEAD")
    assert show["ok"] is True
    assert "second commit" in show["output"] and "hello world" in show["output"]

    blame = module.git_blame(str(repo), file="notes.txt")
    assert blame["ok"] is True and "Tester" in blame["output"]

    branches = module.git_branches(str(repo))
    assert branches["ok"] is True and branches["count"] >= 1
    assert branches["output"].strip()

    relative = module.git_status("repo")
    assert relative["ok"] is True
    assert Path(relative["repo"]) == (roots / "repo").resolve()


def test_git_rejects_unconfigured_outside_and_non_repo(git_env, tmp_path, monkeypatch):
    module, roots, repo = git_env

    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init")
    payload = module.git_status(str(outside))
    assert payload["ok"] is False and "越界" in payload["error"]

    payload = module.git_status(str(roots / "not_repo"))
    assert payload["ok"] is False and "不是有效的 git 仓库" in payload["error"]

    sub = repo / "sub"
    sub.mkdir()
    payload = module.git_status(str(sub))
    assert payload["ok"] is False and "仓库根目录" in payload["error"]

    monkeypatch.setenv("GIT_MCP_ROOTS", "")
    unconfigured = {
        "git_status": (str(repo),),
        "git_log": (str(repo),),
        "git_diff": (str(repo),),
        "git_show": (str(repo),),
        "git_blame": (str(repo), "notes.txt"),
        "git_branches": (str(repo),),
        "git_add": (str(repo),),
        "git_commit": (str(repo), "msg"),
        "git_checkout": (str(repo), "main"),
        "git_branch_create": (str(repo), "dev"),
        "git_branch_delete": (str(repo), "dev"),
    }
    for name, args in unconfigured.items():
        payload = getattr(module, name)(*args)
        assert "未配置仓库目录" in payload["error"], name


def test_git_write_disabled_by_default(git_env):
    module, roots, repo = git_env
    assert "写操作未开启" in module.git_add(str(repo), all=True)["error"]
    assert "写操作未开启" in module.git_commit(str(repo), "msg")["error"]
    assert "写操作未开启" in module.git_checkout(str(repo), "main")["error"]
    assert "写操作未开启" in module.git_branch_create(str(repo), "dev")["error"]
    assert "写操作未开启" in module.git_branch_delete(str(repo), "dev")["error"]
    assert not (repo / ".git" / "refs" / "heads" / "dev").exists()


def test_git_write_tools_when_enabled(git_env, monkeypatch):
    module, roots, repo = git_env
    monkeypatch.setenv("GIT_MCP_ALLOW_WRITE", "true")
    original = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    assert module.git_branch_create(str(repo), "feature-x")["ok"] is True
    assert "feature-x" in module.git_branches(str(repo))["output"]

    assert module.git_checkout(str(repo), "feature-y", create=True)["ok"] is True
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature-y"
    assert module.git_checkout(str(repo), original)["ok"] is True

    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    add = module.git_add(str(repo), paths=["new.txt"])
    assert add["ok"] is True and add["paths"] == ["new.txt"]
    commit = module.git_commit(str(repo), "add new file")
    assert commit["ok"] is True
    assert "add new file" in module.git_log(str(repo), limit=1)["output"]

    assert module.git_branch_delete(str(repo), "feature-x")["ok"] is True
    assert module.git_branch_delete(str(repo), "feature-y")["ok"] is True
    assert "feature-x" not in module.git_branches(str(repo))["output"]

    assert module.git_add(str(repo), all=True)["ok"] is True


def test_git_rejects_option_injection_and_path_escape(git_env, monkeypatch):
    module, roots, repo = git_env
    for payload in (
        module.git_show(str(repo), ref="--upload-pack=touch pwned"),
        module.git_log(str(repo), file="-x"),
        module.git_diff(str(repo), ref="--output=pwned"),
        module.git_blame(str(repo), file="--help"),
    ):
        assert payload["ok"] is False and "选项注入" in payload["error"]

    monkeypatch.setenv("GIT_MCP_ALLOW_WRITE", "true")
    assert "选项注入" in module.git_add(str(repo), paths=["-x"])["error"]
    assert "选项注入" in module.git_checkout(str(repo), "--orphan")["error"]
    assert "选项注入" in module.git_branch_create(str(repo), "-D")["error"]
    assert "选项注入" in module.git_branch_delete(str(repo), "--force")["error"]
    assert "不允许包含 '..'" in module.git_add(str(repo), paths=["../escape.txt"])["error"]
    assert "仅允许仓库内相对路径" in module.git_add(str(repo), paths=[str(repo / "hello.txt")])["error"]
    assert not (repo / "pwned").exists()


def test_git_output_truncation(git_env, monkeypatch):
    module, roots, repo = git_env
    monkeypatch.setenv("GIT_MCP_MAX_OUTPUT_CHARS", "20")

    payload = module.git_show(str(repo))
    assert payload["ok"] is True and payload["truncated"] is True
    assert len(payload["output"]) == 20

    payload = module.git_log(str(repo))
    assert payload["ok"] is True and payload["truncated"] is True
    assert len(payload["output"]) == 20


def test_git_commit_requires_message_and_maps_git_error(git_env, monkeypatch):
    module, roots, repo = git_env
    monkeypatch.setenv("GIT_MCP_ALLOW_WRITE", "true")

    payload = module.git_commit(str(repo), "   ")
    assert payload["ok"] is False and "message 不能为空" in payload["error"]

    payload = module.git_commit(str(repo), "no staged changes")
    assert payload["ok"] is False and "git 返回" in payload["error"]


def test_git_bare_repo_allows_reads_and_rejects_worktree_ops(git_env, tmp_path, monkeypatch):
    module, roots, repo = git_env
    bare = roots / "bare.git"
    _git(tmp_path, "clone", "--bare", str(repo), str(bare))

    payload = module.git_log(str(bare))
    assert payload["ok"] is True and "second commit" in payload["output"]

    payload = module.git_branches(str(bare))
    assert payload["ok"] is True

    payload = module.git_status(str(bare))
    assert payload["ok"] is False and "裸仓库" in payload["error"]

    payload = module.git_diff(str(bare))
    assert payload["ok"] is False and "裸仓库" in payload["error"]

    monkeypatch.setenv("GIT_MCP_ALLOW_WRITE", "true")
    worktree_ops = {
        "git_add": (str(bare),),
        "git_commit": (str(bare), "x"),
        "git_checkout": (str(bare), "main"),
    }
    for name, args in worktree_ops.items():
        payload = getattr(module, name)(*args)
        assert payload["ok"] is False and "裸仓库" in payload["error"], name

    assert module.git_branch_create(str(bare), "from-bare")["ok"] is True


async def test_git_server_in_process_client(git_env):
    module, roots, repo = git_env
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 11
        assert {tool.name for tool in tools.tools} >= {"git_status", "git_log", "git_commit"}
        result = await client.call_tool("git_status", {"repo": str(repo)})
    payload = _payload(result)
    assert payload["ok"] is True


# ---------------------------------------------------------------- postgres

def test_postgres_is_read_only_sql_allows_select_forms():
    module = _load("mcp_postgres")
    allowed = [
        "SELECT 1",
        "select * from public.users where id = 1",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "WITH RECURSIVE t(n) AS (VALUES (1)) SELECT n FROM t",
        "EXPLAIN SELECT 1",
        "EXPLAIN (ANALYZE, BUFFERS) SELECT 1",
        "SHOW default_transaction_read_only",
        "VALUES (1), (2)",
        "SELECT 1;",
        "SELECT 1; -- trailing comment",
        "-- leading comment\nSELECT 1",
        "/* leading */ SELECT 1",
        "SELECT ';' AS semi",
        "SELECT $$;$$ AS dollar",
        'SELECT "select" FROM t',
        "SELECT '--' AS dashes",
    ]
    for sql in allowed:
        ok, reason = module.is_read_only_sql(sql)
        assert ok is True, (sql, reason)


def test_postgres_is_read_only_sql_rejects_injection_side_effects():
    module = _load("mcp_postgres")
    rejected = [
        "",
        "   ",
        "select 1; drop table x",
        "SELECT 1; SELECT 2",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "WITH x AS (SELECT 1) SELECT 1; DROP TABLE t",
        "select 1 -- comment\n; drop table t",
        "/* /* */ */ SELECT 1; DROP TABLE t",
        "SELECT '--'; DROP TABLE x",
        "SELECT 1 /* unclosed",
        "SELECT 'unclosed",
        "UPDATE t SET x = 1",
        "DROP TABLE t",
        "CREATE TABLE t (x int)",
        "EXPLAIN ANALYZE DELETE FROM t",
        "SELECT 1 INTO new_table",
        "TRUNCATE t",
        "COPY t TO '/tmp/x'",
        "GRANT SELECT ON t TO u",
        "SELECT 1; \n x",
    ]
    for sql in rejected:
        ok, reason = module.is_read_only_sql(sql)
        assert ok is False, sql
        assert reason


def test_postgres_clamp_limit_and_max_rows(monkeypatch):
    module = _load("mcp_postgres")
    assert module.clamp_limit(0, 200) == 200
    assert module.clamp_limit(50, 200) == 50
    assert module.clamp_limit(500, 200) == 200
    assert module.clamp_limit(-5, 200) == 200
    assert module.clamp_limit("abc", 200) == 200
    assert module.clamp_limit(10, None) == 10

    monkeypatch.setenv("PG_MCP_MAX_ROWS", "5000")
    assert module._max_rows() == 1000
    monkeypatch.setenv("PG_MCP_MAX_ROWS", "150")
    assert module._max_rows() == 150
    monkeypatch.setenv("PG_MCP_MAX_ROWS", "not-a-number")
    assert module._max_rows() == 200


def test_postgres_connect_options_force_readonly_and_timeout(monkeypatch):
    module = _load("mcp_postgres")
    monkeypatch.setenv("PG_MCP_STATEMENT_TIMEOUT_MS", "5000")
    options = module._connect_options()
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=5000" in options

    monkeypatch.setenv("PG_MCP_STATEMENT_TIMEOUT_MS", "abc")
    assert "statement_timeout=10000" in module._connect_options()


@pytest.mark.parametrize("tool,args", [
    ("pg_query", ("SELECT 1",)),
    ("pg_list_tables", ()),
    ("pg_describe_table", ("users",)),
])
def test_postgres_missing_dsn_returns_readable_error(monkeypatch, tool, args):
    module = _load("mcp_postgres")
    monkeypatch.delenv("PG_MCP_DSN", raising=False)
    payload = getattr(module, tool)(*args)
    assert payload["ok"] is False and "未配置连接串" in payload["error"]

    monkeypatch.setenv("PG_MCP_DSN", "${PG_MCP_DSN}")
    payload = getattr(module, tool)(*args)
    assert payload["ok"] is False and "未配置连接串" in payload["error"]


def test_postgres_rejects_side_effect_sql_before_connecting(monkeypatch):
    module = _load("mcp_postgres")
    monkeypatch.setenv("PG_MCP_DSN", "postgresql://u:p@127.0.0.1:1/db")

    def boom(dsn):
        raise AssertionError("禁止连接数据库")

    monkeypatch.setattr(module, "_connect", boom)

    payload = module.pg_query("DROP TABLE users")
    assert payload["ok"] is False and "仅允许" in payload["error"]

    payload = module.pg_query("select 1; drop table x")
    assert payload["ok"] is False and "多条语句" in payload["error"]

    payload = module.pg_query("")
    assert payload["ok"] is False and "不能为空" in payload["error"]


def test_postgres_connection_failure_maps_readable_error(monkeypatch):
    module = _load("mcp_postgres")
    monkeypatch.setenv("PG_MCP_DSN", "postgresql://u:p@127.0.0.1:1/db")

    def refuse(dsn):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(module, "_connect", refuse)
    payload = module.pg_query("SELECT 1")
    assert payload["ok"] is False
    assert "查询失败" in payload["error"] and "connection refused" in payload["error"]


class _FakeDescription:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    @property
    def description(self):
        return [_FakeDescription(name) for name in self.columns]

    def fetchmany(self, size):
        return self.rows[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.exited = False

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True
        return False


def test_postgres_pg_query_clamps_rows_with_fake_connection(monkeypatch):
    module = _load("mcp_postgres")
    monkeypatch.setenv("PG_MCP_DSN", "postgresql://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("PG_MCP_MAX_ROWS", "2")
    cursor = _FakeCursor(["id", "name"], [[1, "a"], [2, "b"], [3, "c"]])
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(module, "_connect", lambda dsn: conn)

    payload = module.pg_query("SELECT id, name FROM t", limit=0)
    assert payload["ok"] is True
    assert payload["columns"] == ["id", "name"]
    assert payload["rows"] == [[1, "a"], [2, "b"]]
    assert payload["row_count"] == 2
    assert payload["truncated"] is True
    assert payload["limit"] == 2
    assert cursor.executed[0] == ("SELECT id, name FROM t", None)
    assert conn.exited is True


async def test_postgres_server_in_process_client(monkeypatch):
    module = _load("mcp_postgres")
    monkeypatch.delenv("PG_MCP_DSN", raising=False)
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 3
        assert {tool.name for tool in tools.tools} == {"pg_query", "pg_list_tables", "pg_describe_table"}
        result = await client.call_tool("pg_query", {"sql": "select 1"})
    payload = _payload(result)
    assert payload["ok"] is False and "未配置连接串" in payload["error"]
