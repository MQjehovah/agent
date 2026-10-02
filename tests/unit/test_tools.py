"""工具单元测试"""
import asyncio
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


# ═══════════════════════════════════════════════════════════
#  EditTool
# ═══════════════════════════════════════════════════════════

class TestEditTool:
    def setup_method(self):
        from tools.edit import EditTool
        self.tool = EditTool()

    @pytest.mark.asyncio
    async def test_replace_unique(self, tmp_path):
        test_file = tmp_path / "edit.txt"
        test_file.write_text("hello world\nfoo bar\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="hello world",
            new_string="hello python"
        )
        data = json.loads(r)
        assert data["success"] is True
        assert "hello python" in test_file.read_text()

    @pytest.mark.asyncio
    async def test_replace_all(self, tmp_path):
        test_file = tmp_path / "multi.txt"
        test_file.write_text("aaa\naaa\naaa\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="aaa",
            new_string="bbb",
            replace_all=True
        )
        data = json.loads(r)
        assert data["success"] is True
        assert data["replacements"] == 3

    @pytest.mark.asyncio
    async def test_no_match(self, tmp_path):
        test_file = tmp_path / "nomatch.txt"
        test_file.write_text("hello\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="nonexistent",
            new_string="replacement"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "未找到" in data["error"]

    @pytest.mark.asyncio
    async def test_ambiguous_match(self, tmp_path):
        test_file = tmp_path / "ambiguous.txt"
        test_file.write_text("aaa\naaa\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="aaa",
            new_string="bbb"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "2 处匹配" in data["error"]

    @pytest.mark.asyncio
    async def test_trailing_whitespace_tolerated(self, tmp_path):
        """文件有尾部空白，old_string 没有尾部空白，应该仍能匹配"""
        test_file = tmp_path / "trailing.txt"
        test_file.write_text("def hello():   \n    print('hi')   \n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="def hello():\n    print('hi')",
            new_string="def hello():\n    print('hello')"
        )
        data = json.loads(r)
        assert data["success"] is True
        result = test_file.read_text()
        assert "print('hello')" in result
        assert "print('hi')" not in result

    @pytest.mark.asyncio
    async def test_crlf_tolerated(self, tmp_path):
        """文件使用 CRLF 换行，old_string 使用 LF，应该仍能匹配"""
        test_file = tmp_path / "crlf.txt"
        test_file.write_bytes(b"line one\r\nline two\r\nline three\r\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="line two",
            new_string="line 2"
        )
        data = json.loads(r)
        assert data["success"] is True
        assert "line 2" in test_file.read_text()

    @pytest.mark.asyncio
    async def test_old_string_with_trailing_whitespace(self, tmp_path):
        """old_string 有尾部空白，文件没有，也应该匹配"""
        test_file = tmp_path / "clean.txt"
        test_file.write_text("hello world\nfoo bar\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="hello world   ",
            new_string="hello python"
        )
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_multiline_edit(self, tmp_path):
        """多行替换"""
        test_file = tmp_path / "multi_line.py"
        test_file.write_text("def foo():\n    pass\n\ndef bar():\n    pass\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="def foo():\n    pass",
            new_string="def foo():\n    return 42"
        )
        data = json.loads(r)
        assert data["success"] is True
        content = test_file.read_text()
        assert "return 42" in content
        assert "def bar()" in content

    @pytest.mark.asyncio
    async def test_chinese_content(self, tmp_path):
        """中文内容编辑"""
        test_file = tmp_path / "chinese.txt"
        test_file.write_text("你好世界\n欢迎使用\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="你好世界",
            new_string="你好Python"
        )
        data = json.loads(r)
        assert data["success"] is True
        assert "你好Python" in test_file.read_text()

    @pytest.mark.asyncio
    async def test_special_chars(self, tmp_path):
        """特殊字符内容"""
        test_file = tmp_path / "special.txt"
        test_file.write_text('value = "hello\\nworld"\n')

        r = await self.tool.execute(
            path=str(test_file),
            old_string='value = "hello\\nworld"',
            new_string='value = "hello\\npython"'
        )
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_empty_old_string(self, tmp_path):
        test_file = tmp_path / "empty.txt"
        test_file.write_text("content\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="",
            new_string="new_string"
        )
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_same_old_new(self, tmp_path):
        test_file = tmp_path / "same.txt"
        test_file.write_text("abc\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="abc",
            new_string="abc"
        )
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        r = await self.tool.execute(
            path="/nonexistent/file.txt",
            old_string="foo",
            new_string="bar"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "文件不存在" in data["error"]

    @pytest.mark.asyncio
    async def test_directory_path(self, tmp_path):
        r = await self.tool.execute(
            path=str(tmp_path),
            old_string="foo",
            new_string="bar"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "目录" in data["error"]

    @pytest.mark.asyncio
    async def test_mismatch_hint(self, tmp_path):
        """匹配失败时提供相似内容提示"""
        test_file = tmp_path / "hint.py"
        test_file.write_text("def hello_world():\n    print('hello')\n    return True\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="def hello_worl():\n    print('hello')",
            new_string="def hello():\n    pass"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "hint" in data
        assert len(data["hint"]) > 0
        assert "hello_world" in data["hint"]

    @pytest.mark.asyncio
    async def test_empty_path(self, tmp_path):
        r = await self.tool.execute(path="", old_string="a", new_string="b")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_replace_first_occurrence_only(self, tmp_path):
        """非 replace_all 模式只替换第一个匹配"""
        test_file = tmp_path / "first.txt"
        test_file.write_text("prefix_aaa\nbbb\nsuffix_aaa\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="prefix_aaa",
            new_string="ccc",
            replace_all=False
        )
        data = json.loads(r)
        assert data["success"] is True
        content = test_file.read_text()
        assert content.startswith("ccc\nbbb\nsuffix_aaa\n")

    @pytest.mark.asyncio
    async def test_indentation_sensitive(self, tmp_path):
        """缩进敏感：不同缩进不算匹配"""
        test_file = tmp_path / "indent.py"
        test_file.write_text("def foo():\n    pass\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="pass",
            new_string="return 1"
        )
        data = json.loads(r)
        assert data["success"] is True
        content = test_file.read_text()
        assert "    return 1" in content

    @pytest.mark.asyncio
    async def test_line_anchor_targets_nearest_occurrence(self, tmp_path):
        """行号锚点应命中锚点最近的一处，而不是文件首个匹配"""
        test_file = tmp_path / "anchor.txt"
        test_file.write_text("x = 1\ndo()\nx = 1\ndo()\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="x = 1",
            new_string="x = 2",
            line=3,
        )
        data = json.loads(r)
        assert data["success"] is True
        assert data["replacements"] == 1
        assert test_file.read_text() == "x = 1\ndo()\nx = 2\ndo()\n"

    @pytest.mark.asyncio
    async def test_preserves_crlf_and_trailing_whitespace(self, tmp_path):
        """编辑只动命中区间：不重写整文件，保留 CRLF 与他人行尾空白"""
        test_file = tmp_path / "preserve.txt"
        test_file.write_bytes(b"a  \r\nb  \r\nc\r\n")

        r = await self.tool.execute(path=str(test_file), old_string="b", new_string="B")
        assert json.loads(r)["success"] is True
        assert test_file.read_bytes() == b"a  \r\nB  \r\nc\r\n"

    @pytest.mark.asyncio
    async def test_whitespace_collapse_match_changes_content(self, tmp_path):
        """折叠空白兜底命中后必须真实改写（不产生空 diff 的假成功）"""
        test_file = tmp_path / "collapse.txt"
        test_file.write_text("value   =   1\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_string="value = 1",
            new_string="value = 2",
        )
        data = json.loads(r)
        assert data["success"] is True
        assert test_file.read_text() == "value = 2\n"

    @pytest.mark.asyncio
    async def test_batch_preserves_untouched_lines(self, tmp_path):
        """批量编辑逐条按偏移替换，未命中行原样保留"""
        test_file = tmp_path / "batch.txt"
        test_file.write_text("keep   \nfoo()\nkeep2\n")

        r = await self.tool.execute(
            path=str(test_file),
            edits=[{"old_string": "foo()", "new_string": "bar()"}],
        )
        data = json.loads(r)
        assert data["success"] is True
        assert test_file.read_text() == "keep   \nbar()\nkeep2\n"


# ═══════════════════════════════════════════════════════════
#  FileTool
# ═══════════════════════════════════════════════════════════

class TestFileTool:
    def setup_method(self):
        from tools.file import FileTool
        self.tool = FileTool()

    @pytest.mark.asyncio
    async def test_write_and_read(self, tmp_path):
        test_file = str(tmp_path / "test.txt")
        r = await self.tool.execute(operation="write", path=test_file, content="hello world")
        assert json.loads(r)["success"] is True

        r = await self.tool.execute(operation="read", path=test_file)
        data = json.loads(r)
        assert data["success"] is True
        assert "hello world" in data["content"]

    @pytest.mark.asyncio
    async def test_read_with_line_numbers(self, tmp_path):
        test_file = str(tmp_path / "lines.txt")
        with open(test_file, "w") as f:
            f.write("line1\nline2\nline3\nline4\nline5\n")

        r = await self.tool.execute(operation="read", path=test_file, offset=1, limit=2)
        data = json.loads(r)
        assert data["success"] is True
        assert data["showing"] == "2-3"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        r = await self.tool.execute(operation="read", path="/nonexistent/file.txt")
        assert json.loads(r)["success"] is False

    @pytest.mark.asyncio
    async def test_append(self, tmp_path):
        test_file = str(tmp_path / "append.txt")
        await self.tool.execute(operation="write", path=test_file, content="first\n")
        r = await self.tool.execute(operation="append", path=test_file, content="second\n")
        assert json.loads(r)["success"] is True

        r = await self.tool.execute(operation="read", path=test_file)
        content = json.loads(r)["content"]
        assert "first" in content
        assert "second" in content

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path):
        test_file = str(tmp_path / "delete_me.txt")
        with open(test_file, "w") as f:
            f.write("delete me")
        r = await self.tool.execute(operation="delete", path=test_file)
        assert json.loads(r)["success"] is True
        assert not os.path.exists(test_file)

    @pytest.mark.asyncio
    async def test_exists(self, tmp_path):
        test_file = str(tmp_path / "exists.txt")
        r = await self.tool.execute(operation="exists", path=test_file)
        assert json.loads(r)["exists"] is False

        with open(test_file, "w") as f:
            f.write("exists")
        r = await self.tool.execute(operation="exists", path=test_file)
        assert json.loads(r)["exists"] is True

    @pytest.mark.asyncio
    async def test_list_directory(self, tmp_path):
        (tmp_path / "file1.txt").write_text("a")
        (tmp_path / "file2.txt").write_text("bb")
        r = await self.tool.execute(operation="list", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is True
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_write_creates_dirs(self, tmp_path):
        test_file = str(tmp_path / "sub" / "dir" / "file.txt")
        r = await self.tool.execute(operation="write", path=test_file, content="deep")
        data = json.loads(r)
        assert data["success"] is True
        assert os.path.exists(test_file)

    @pytest.mark.asyncio
    async def test_read_empty_file(self, tmp_path):
        test_file = str(tmp_path / "empty.txt")
        with open(test_file, "w"):
            pass

        r = await self.tool.execute(operation="read", path=test_file)
        data = json.loads(r)
        assert data["success"] is True
        assert data["total_lines"] == 0

    @pytest.mark.asyncio
    async def test_write_without_content(self, tmp_path):
        test_file = str(tmp_path / "no_content.txt")
        r = await self.tool.execute(operation="write", path=test_file)
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_unknown_operation(self, tmp_path):
        test_file = str(tmp_path / "test.txt")
        r = await self.tool.execute(operation="invalid_op", path=test_file)
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_read_directory_fails(self, tmp_path):
        r = await self.tool.execute(operation="read", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is False
        assert "目录" in data["error"]

    @pytest.mark.asyncio
    async def test_delete_directory(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "file.txt").write_text("x")

        r = await self.tool.execute(operation="delete", path=str(sub))
        data = json.loads(r)
        assert data["success"] is True
        assert not os.path.exists(str(sub))

    @pytest.mark.asyncio
    async def test_list_with_directories(self, tmp_path):
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "subdir").mkdir()

        r = await self.tool.execute(operation="list", path=str(tmp_path))
        data = json.loads(r)
        assert data["count"] == 2
        names = [item["name"] for item in data["items"]]
        assert "file.txt" in names
        assert "subdir" in names

    @pytest.mark.asyncio
    async def test_chinese_content(self, tmp_path):
        test_file = str(tmp_path / "chinese.txt")
        r = await self.tool.execute(operation="write", path=test_file, content="你好世界")
        assert json.loads(r)["success"] is True

        r = await self.tool.execute(operation="read", path=test_file)
        data = json.loads(r)
        assert "你好世界" in data["content"]


# ═══════════════════════════════════════════════════════════
#  ShellTool
# ═══════════════════════════════════════════════════════════

class TestShellTool:
    def setup_method(self):
        from tools.shell import ShellTool
        self.tool = ShellTool()

    @pytest.mark.asyncio
    async def test_simple_command(self):
        r = await self.tool.execute(command="echo hello")
        data = json.loads(r)
        assert data["success"] is True
        assert "hello" in data["stdout"]

    @pytest.mark.asyncio
    async def test_timeout(self):
        r = await self.tool.execute(command="sleep 10", timeout=1)
        data = json.loads(r)
        assert data["success"] is False
        assert "超时" in data["error"]

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self):
        r = await self.tool.execute(command="rm -rf /")
        data = json.loads(r)
        assert data["success"] is False
        assert "拦截" in data["error"]

    @pytest.mark.asyncio
    async def test_empty_command(self):
        r = await self.tool.execute(command="")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        r = await self.tool.execute(command="exit 1")
        data = json.loads(r)
        assert data["success"] is False
        assert data["return_code"] == 1

    @pytest.mark.asyncio
    async def test_stderr_capture(self):
        r = await self.tool.execute(command="echo error >&2")
        data = json.loads(r)
        assert "error" in data["stderr"]

    @pytest.mark.asyncio
    async def test_cwd(self, tmp_path):
        r = await self.tool.execute(command="pwd", cwd=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is True
        assert str(tmp_path) in data["stdout"].strip()

    @pytest.mark.asyncio
    async def test_env_vars(self):
        r = await self.tool.execute(command="echo $MY_TEST_VAR", env={"MY_TEST_VAR": "test_value"})
        data = json.loads(r)
        assert data["success"] is True
        assert "test_value" in data["stdout"]

    @pytest.mark.asyncio
    async def test_output_truncation(self):
        r = await self.tool.execute(command="python3 -c \"print('x' * 20000)\"", max_chars=100)
        data = json.loads(r)
        assert "截断" in data["stdout"]

    @pytest.mark.asyncio
    async def test_chinese_output(self):
        r = await self.tool.execute(command="echo '你好世界'")
        data = json.loads(r)
        assert data["success"] is True
        assert "你好" in data["stdout"]

    @pytest.mark.asyncio
    async def test_multiple_dangerous_commands(self):
        for cmd in ["rm -rf /*", "mkfs", "dd if=/dev/zero of=/dev/sda"]:
            r = await self.tool.execute(command=cmd)
            data = json.loads(r)
            assert data["success"] is False


# ═══════════════════════════════════════════════════════════
#  GrepTool
# ═══════════════════════════════════════════════════════════

class TestGrepTool:
    def setup_method(self):
        from tools.grep import GrepTool
        self.tool = GrepTool()

    @pytest.mark.asyncio
    async def test_search_pattern(self, tmp_path):
        test_file = tmp_path / "code.py"
        test_file.write_text("def hello():\n    print('world')\n\ndef goodbye():\n    pass\n")

        r = await self.tool.execute(pattern="def", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is True
        assert data["total_matches"] == 2

    @pytest.mark.asyncio
    async def test_case_insensitive(self, tmp_path):
        test_file = tmp_path / "mixed.txt"
        test_file.write_text("Hello World\nhello world\nHELLO WORLD\n")

        r = await self.tool.execute(pattern="hello", path=str(tmp_path), case_insensitive=True)
        data = json.loads(r)
        assert data["total_matches"] == 3

    @pytest.mark.asyncio
    async def test_file_pattern_filter(self, tmp_path):
        (tmp_path / "code.py").write_text("import os\n")
        (tmp_path / "data.json").write_text('{"import": true}\n')

        r = await self.tool.execute(pattern="import", path=str(tmp_path), file_pattern="*.py")
        data = json.loads(r)
        assert data["total_matches"] == 1

    @pytest.mark.asyncio
    async def test_no_results(self, tmp_path):
        (tmp_path / "empty.txt").write_text("nothing here")

        r = await self.tool.execute(pattern="nonexistent_pattern_xyz", path=str(tmp_path))
        data = json.loads(r)
        assert data["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_empty_pattern(self, tmp_path):
        r = await self.tool.execute(pattern="", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_invalid_regex(self, tmp_path):
        r = await self.tool.execute(pattern="[invalid", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is False
        assert "正则" in data["error"]

    @pytest.mark.asyncio
    async def test_nonexistent_path(self):
        r = await self.tool.execute(pattern="test", path="/nonexistent/path")
        data = json.loads(r)
        assert data["success"] is False
        assert "不存在" in data["error"]

    @pytest.mark.asyncio
    async def test_single_file_search(self, tmp_path):
        test_file = tmp_path / "single.py"
        test_file.write_text("def hello():\n    pass\n\ndef world():\n    pass\n")

        r = await self.tool.execute(pattern="def", path=str(test_file))
        data = json.loads(r)
        assert data["success"] is True
        assert data["total_matches"] == 2
        assert data["files_searched"] == 1

    @pytest.mark.asyncio
    async def test_single_file_with_file_pattern(self, tmp_path):
        test_file = tmp_path / "code.py"
        test_file.write_text("import os\n")

        r = await self.tool.execute(pattern="import", path=str(test_file), file_pattern="*.txt")
        data = json.loads(r)
        assert data["success"] is True
        assert data["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_max_results(self, tmp_path):
        test_file = tmp_path / "many.txt"
        test_file.write_text("match\n" * 20)

        r = await self.tool.execute(pattern="match", path=str(tmp_path), limit=5)
        data = json.loads(r)
        assert data["total_matches"] <= 5
        assert data["truncated"] is True

    @pytest.mark.asyncio
    async def test_recursive_search(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.py").write_text("import os\n")
        (sub / "child.py").write_text("import sys\n")

        r = await self.tool.execute(pattern="import", path=str(tmp_path))
        data = json.loads(r)
        assert data["total_matches"] == 2

    @pytest.mark.asyncio
    async def test_context_lines(self, tmp_path):
        test_file = tmp_path / "ctx.txt"
        test_file.write_text("line1\nline2\nTARGET\nline4\nline5\n")

        r = await self.tool.execute(pattern="TARGET", path=str(tmp_path), context_lines=1)
        data = json.loads(r)
        assert data["total_matches"] == 1
        ctx = data["matches"][0].get("context", "")
        assert "line2" in ctx
        assert "line4" in ctx


# ═══════════════════════════════════════════════════════════
#  GlobTool
# ═══════════════════════════════════════════════════════════

class TestGlobTool:
    def setup_method(self):
        from tools.glob import GlobTool
        self.tool = GlobTool()

    @pytest.mark.asyncio
    async def test_recursive_glob(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "a.py").write_text("")
        (sub / "b.py").write_text("")
        (sub / "c.txt").write_text("")

        r = await self.tool.execute(pattern="**/*.py", path=str(tmp_path))
        data = json.loads(r)
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_simple_pattern(self, tmp_path):
        (tmp_path / "readme.md").write_text("")
        (tmp_path / "code.py").write_text("")

        r = await self.tool.execute(pattern="*.md", path=str(tmp_path))
        data = json.loads(r)
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_empty_pattern(self, tmp_path):
        r = await self.tool.execute(pattern="", path=str(tmp_path))
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_nonexistent_path(self):
        r = await self.tool.execute(pattern="*", path="/nonexistent")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_max_results(self, tmp_path):
        for i in range(20):
            (tmp_path / f"file_{i:02d}.txt").write_text("")

        r = await self.tool.execute(pattern="*.txt", path=str(tmp_path), limit=5)
        data = json.loads(r)
        assert data["count"] == 5

    @pytest.mark.asyncio
    async def test_no_match(self, tmp_path):
        (tmp_path / "a.py").write_text("")

        r = await self.tool.execute(pattern="*.java", path=str(tmp_path))
        data = json.loads(r)
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_nested_recursive(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("")

        r = await self.tool.execute(pattern="**/*.py", path=str(tmp_path))
        data = json.loads(r)
        assert data["count"] == 1
        assert "deep.py" in data["files"][0]


# ═══════════════════════════════════════════════════════════
#  TodoTool
# ═══════════════════════════════════════════════════════════

class TestTodoTool:
    def setup_method(self):
        from tools.todo import TodoTool
        self.tool = TodoTool()

    @pytest.mark.asyncio
    async def test_add_todo(self):
        r = await self.tool.execute(todos=[{"content": "任务一"}])
        data = json.loads(r)
        assert data["success"] is True
        assert data["filtered_count"] == 1

    @pytest.mark.asyncio
    async def test_add_multiple(self):
        r = await self.tool.execute(todos=[
            {"content": "任务一"},
            {"content": "任务二"},
            {"content": "任务三"},
        ])
        data = json.loads(r)
        assert data["success"] is True
        assert data["filtered_count"] == 3

    @pytest.mark.asyncio
    async def test_update_status(self):
        r = await self.tool.execute(todos=[{"content": "任务一"}])
        todo_id = json.loads(r)["todos"][0]["id"]

        r = await self.tool.execute(todos=[{"id": todo_id, "status": "completed"}])
        data = json.loads(r)
        assert data["success"] is True

        todos = self.tool.get_todos()
        assert todos[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_update_priority(self):
        r = await self.tool.execute(todos=[{"content": "任务一"}])
        todo_id = json.loads(r)["todos"][0]["id"]

        r = await self.tool.execute(todos=[{"id": todo_id, "priority": "high"}])
        data = json.loads(r)
        assert data["success"] is True

        todos = self.tool.get_todos()
        assert todos[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        await self.tool.execute(todos=[
            {"content": "pending_task"},
            {"content": "done_task", "status": "completed"},
        ])

        r = await self.tool.execute(todos=[], filter_status="completed")
        data = json.loads(r)
        assert data["filtered_count"] == 1
        assert data["todos"][0]["content"] == "done_task"

    @pytest.mark.asyncio
    async def test_filter_all(self):
        await self.tool.execute(todos=[
            {"content": "t1", "status": "pending"},
            {"content": "t2", "status": "completed"},
        ])

        r = await self.tool.execute(todos=[], filter_status="all")
        data = json.loads(r)
        assert data["filtered_count"] == 2

    @pytest.mark.asyncio
    async def test_clear_completed(self):
        await self.tool.execute(todos=[
            {"content": "t1", "status": "completed"},
            {"content": "t2", "status": "pending"},
        ])

        count = self.tool.clear_completed()
        assert count == 1
        assert len(self.tool.get_todos()) == 1

    @pytest.mark.asyncio
    async def test_clear_all(self):
        await self.tool.execute(todos=[{"content": "t1"}, {"content": "t2"}])
        count = self.tool.clear_all()
        assert count == 2
        assert len(self.tool.get_todos()) == 0

    @pytest.mark.asyncio
    async def test_add_with_status_and_priority(self):
        r = await self.tool.execute(todos=[{
            "content": "urgent task",
            "status": "in_progress",
            "priority": "high"
        }])
        data = json.loads(r)
        assert data["success"] is True
        todo = data["todos"][0]
        assert todo["status"] == "in_progress"
        assert todo["priority"] == "high"

    @pytest.mark.asyncio
    async def test_invalid_id_update_ignored(self):
        r = await self.tool.execute(todos=[{"id": "nonexistent", "status": "completed"}])
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_persistence(self, tmp_path):
        from tools.todo import TodoTool
        persist_path = str(tmp_path / "todos.json")
        tool1 = TodoTool(persist_path=persist_path)
        await tool1.execute(todos=[{"content": "persistent task"}])

        tool2 = TodoTool(persist_path=persist_path)
        todos = tool2.get_todos()
        assert len(todos) == 1
        assert todos[0]["content"] == "persistent task"


# ═══════════════════════════════════════════════════════════
#  TaskTool (TaskManager, Create, List, Get, Cancel)
# ═══════════════════════════════════════════════════════════

class TestTaskManager:
    def setup_method(self):
        from tools.task import TaskCancelTool, TaskCreateTool, TaskGetTool, TaskListTool, TaskManager
        self.manager = TaskManager()
        self.create_tool = TaskCreateTool(self.manager)
        self.list_tool = TaskListTool(self.manager)
        self.get_tool = TaskGetTool(self.manager)
        self.cancel_tool = TaskCancelTool(self.manager)

    @pytest.mark.asyncio
    async def test_create_task(self):
        r = await self.create_tool.execute(description="测试任务")
        data = json.loads(r)
        assert data["success"] is True
        assert data["status"] == "pending"
        assert data["description"] == "测试任务"

    @pytest.mark.asyncio
    async def test_list_tasks(self):
        await self.create_tool.execute(description="任务1")
        await self.create_tool.execute(description="任务2")

        r = await self.list_tool.execute()
        data = json.loads(r)
        assert data["success"] is True
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_get_task(self):
        r = await self.create_tool.execute(description="获取测试")
        task_id = json.loads(r)["task_id"]

        r = await self.get_tool.execute(task_id=task_id)
        data = json.loads(r)
        assert data["success"] is True
        assert data["task"]["description"] == "获取测试"

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self):
        r = await self.get_tool.execute(task_id="nonexistent")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_run_task_to_completion(self):
        task = self.manager.create_task("完成测试")

        async def simple_coro():
            return "done"

        await self.manager.start_task(task.id, simple_coro())
        await asyncio.sleep(0.1)

        updated = self.manager.get_task(task.id)
        assert updated.status == "completed"
        assert updated.result == "done"

    @pytest.mark.asyncio
    async def test_run_task_failure(self):
        task = self.manager.create_task("失败测试")

        async def failing_coro():
            raise ValueError("intentional error")

        await self.manager.start_task(task.id, failing_coro())
        await asyncio.sleep(0.1)

        updated = self.manager.get_task(task.id)
        assert updated.status == "failed"
        assert "intentional error" in updated.error

    @pytest.mark.asyncio
    async def test_cancel_task(self):
        task = self.manager.create_task("取消测试")

        async def long_coro():
            await asyncio.sleep(100)

        await self.manager.start_task(task.id, long_coro())

        r = await self.cancel_tool.execute(task_id=task.id)
        data = json.loads(r)
        assert data["success"] is True

        await asyncio.sleep(0.1)
        updated = self.manager.get_task(task.id)
        assert updated.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        r = await self.cancel_tool.execute(task_id="nonexistent")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_cleanup_completed(self):
        for i in range(5):
            task = self.manager.create_task(f"task_{i}")
            task.status = "completed"
        for i in range(3):
            self.manager.create_task(f"running_{i}")

        self.manager.cleanup_completed(max_keep=2)
        tasks = self.manager.list_tasks()
        completed = [t for t in tasks if t["status"] == "completed"]
        assert len(completed) == 2


# ═══════════════════════════════════════════════════════════
#  AskUserTool
# ═══════════════════════════════════════════════════════════

class TestAskUserTool:
    def setup_method(self):
        from tools.ask_user import AskUserTool
        self.tool = AskUserTool()

    @pytest.mark.asyncio
    async def test_with_handler(self):
        async def handler(question, options, default):
            return "user_answer"

        self.tool.set_input_handler(handler)
        r = await self.tool.execute(question="测试问题")
        data = json.loads(r)
        assert data["success"] is True
        assert data["answer"] == "user_answer"

    @pytest.mark.asyncio
    async def test_handler_with_options(self):
        async def handler(question, options, default):
            return options[0]

        self.tool.set_input_handler(handler)
        r = await self.tool.execute(question="选择", options=["A", "B", "C"])
        data = json.loads(r)
        assert data["success"] is True
        assert data["answer"] == "A"

    @pytest.mark.asyncio
    async def test_handler_exception_uses_default(self):
        async def handler(question, options, default):
            raise RuntimeError("handler error")

        self.tool.set_input_handler(handler)
        r = await self.tool.execute(question="测试", default="fallback")
        data = json.loads(r)
        assert data["success"] is True
        assert data["answer"] == "fallback"
        assert data["auto"] is True

    @pytest.mark.asyncio
    async def test_empty_question(self):
        r = await self.tool.execute(question="")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_handler_returns_default(self):
        async def handler(question, options, default):
            return default

        self.tool.set_input_handler(handler)
        r = await self.tool.execute(question="测试", default="my_default")
        data = json.loads(r)
        assert data["success"] is True
        assert data["answer"] == "my_default"


# ═══════════════════════════════════════════════════════════
#  MemoryTool
# ═══════════════════════════════════════════════════════════

class TestMemoryTool:
    def setup_method(self):
        from unittest.mock import MagicMock

        from tools.memory import MemoryTool
        self.mock_manager = MagicMock()
        self.tool = MemoryTool(self.mock_manager)

    @pytest.mark.asyncio
    async def test_save_key_info(self):
        self.mock_manager.add_key_info = MagicMock()
        r = await self.tool.execute(action="save", content="重要信息", category="key_info", _local_user_id="userA")
        data = json.loads(r)
        assert data["success"] is True
        self.mock_manager.add_key_info.assert_called_once_with("userA", "重要信息")

    @pytest.mark.asyncio
    async def test_save_without_content(self):
        r = await self.tool.execute(action="save", category="key_info", _local_user_id="userA")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_search(self):
        self.mock_manager.load_memory = MagicMock(return_value=["记忆1"])
        r = await self.tool.execute(action="search", query="测试", _local_user_id="userA")
        data = json.loads(r)
        assert data["success"] is True
        assert len(data["results"]) == 1

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        self.mock_manager.load_memory = MagicMock(return_value=[])
        r = await self.tool.execute(action="search", query="不存在", _local_user_id="userA")
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_list_daily(self):
        r = await self.tool.execute(action="list", memory_type="daily")
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_no_manager(self):
        from tools.memory import MemoryTool
        tool = MemoryTool()
        r = await tool.execute(action="save", content="test")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        r = await self.tool.execute(action="invalid")
        data = json.loads(r)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_save_preference(self):
        self.mock_manager.add_preference = MagicMock()
        r = await self.tool.execute(action="save", content="喜欢简洁", category="preference", _local_user_id="userA")
        data = json.loads(r)
        assert data["success"] is True
        self.mock_manager.add_preference.assert_called_once_with("userA", "喜欢简洁")


# ═══════════════════════════════════════════════════════════
#  SubagentTool
# ═══════════════════════════════════════════════════════════

class TestSubagentTool:
    def setup_method(self):
        from tools.subagent import SubagentTool
        self.tool = SubagentTool()

    @pytest.mark.asyncio
    async def test_execute_requires_kernel(self):
        """task 由内核调度；直接 execute 应返回明确错误而非回显参数。"""
        r = await self.tool.execute(task="测试任务", template="analyst")
        data = json.loads(r)
        assert data["success"] is False
        assert "内核" in data["error"]

    def test_name(self):
        assert self.tool.name == "task"

    def test_has_required_params(self):
        params = self.tool.parameters
        assert "task" in params["properties"]
        assert "task" in params["required"]


# ═══════════════════════════════════════════════════════════
#  ToolRegistry
# ═══════════════════════════════════════════════════════════

class TestToolRegistry:
    def setup_method(self):
        from tools import ToolRegistry
        self.registry = ToolRegistry()

    def test_register_and_has_tool(self):
        from tools.edit import EditTool
        tool = EditTool()
        self.registry.register_tool(tool)
        assert self.registry.has_tool("edit")

    def test_unregister_tool(self):
        from tools.edit import EditTool
        tool = EditTool()
        self.registry.register_tool(tool)
        assert self.registry.unregister_tool("edit") is True
        assert not self.registry.has_tool("edit")

    def test_unregister_nonexistent(self):
        assert self.registry.unregister_tool("nonexistent") is False

    def test_list_tools(self):
        from tools.edit import EditTool
        from tools.shell import ShellTool
        self.registry.register_tool(EditTool())
        self.registry.register_tool(ShellTool())
        tools = self.registry.list_tools()
        assert "edit" in tools
        assert "shell" in tools

    @pytest.mark.asyncio
    async def test_execute_tool(self, tmp_path):
        from tools.edit import EditTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\n")
        self.registry.register_tool(EditTool())

        r = await self.registry.execute("edit", {
            "path": str(test_file),
            "old_string": "hello",
            "new_string": "world"
        })
        data = json.loads(r)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self):
        r = await self.registry.execute("nonexistent", {})
        assert "未找到" in r or "不存在" in r

    def test_get_tool_definitions(self):
        from tools.edit import EditTool
        self.registry.register_tool(EditTool())
        defs = self.registry.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "edit"

    def test_get_tool(self):
        from tools.edit import EditTool
        tool = EditTool()
        self.registry.register_tool(tool)
        assert self.registry.get_tool("edit") is tool
        assert self.registry.get_tool("nonexistent") is None


# ═══════════════════════════════════════════════════════════
#  PermissionChecker (existing)
# ═══════════════════════════════════════════════════════════

class TestPermissionChecker:
    def setup_method(self):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        self.auto_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.AUTO))
        self.default_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.DEFAULT))
        self.plan_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.PLAN))

    def test_auto_mode_allows_all(self):
        result = self.auto_checker.check("shell", {"command": "rm -rf /"})
        assert result.allowed is True

    def test_plan_mode_blocks_writes(self):
        result = self.plan_checker.check("file", {"operation": "write", "path": "/tmp/test"})
        assert result.allowed is False

    def test_plan_mode_allows_reads(self):
        result = self.plan_checker.check("file", {"operation": "read", "path": "/tmp/test"})
        assert result.allowed is True

    def test_default_mode_needs_confirm_for_writes(self):
        result = self.default_checker.check("file", {"operation": "write", "path": "/tmp/test"})
        assert result.allowed is True
        assert "确认" in result.reason

    def test_default_mode_allows_reads(self):
        result = self.default_checker.check("file", {"operation": "read", "path": "/tmp/test"})
        assert result.allowed is True
        assert result.reason == ""

    def test_default_mode_allows_read_commands(self):
        result = self.default_checker.check("shell", {"command": "ls -la"})
        assert result.allowed is True
        assert result.reason == ""

    def test_smart_mode_allows_normal_writes(self):
        """必要时询问: 普通写文件不打扰用户"""
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        checker = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART))
        result = checker.check("file", {"operation": "write", "path": "/tmp/test"})
        assert result.allowed is True
        assert result.reason == ""

    def test_smart_mode_allows_read_commands(self):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        checker = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART))
        result = checker.check("shell", {"command": "ls -la"})
        assert result.allowed is True
        assert result.reason == ""

    def test_smart_mode_asks_before_dangerous_command(self):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        checker = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART))
        result = checker.check("shell", {"command": "rm -rf build"})
        assert result.allowed is True
        assert "确认" in result.reason

    def test_smart_mode_asks_before_delete(self):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        checker = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART))
        result = checker.check("file", {"operation": "delete", "path": "/tmp/test"})
        assert result.allowed is True
        assert "确认" in result.reason

    def test_smart_mode_asks_when_writing_outside_workspace(self, tmp_path):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        ws = tmp_path / "ws"
        ws.mkdir()
        checker = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART, workspace_root=str(ws)))
        inside = checker.check("file", {"operation": "write", "path": str(ws / "a.txt")})
        assert inside.reason == ""
        outside = checker.check("file", {"operation": "write", "path": str(tmp_path / "other.txt")})
        assert "确认" in outside.reason

    def test_git_permission_by_operation(self):
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        default = PermissionChecker(PermissionConfig(mode=PermissionMode.DEFAULT))
        plan = PermissionChecker(PermissionConfig(mode=PermissionMode.PLAN))
        smart = PermissionChecker(PermissionConfig(mode=PermissionMode.SMART))

        # default: 只读放行，写需确认
        assert default.check("git", {"operation": "status"}).reason == ""
        assert "确认" in default.check("git", {"operation": "commit", "message": "x"}).reason
        # plan: 只读放行，写阻断
        assert plan.check("git", {"operation": "log"}).allowed is True
        assert plan.check("git", {"operation": "commit", "message": "x"}).allowed is False
        # smart: rollback 需确认，commit 放行
        assert "确认" in smart.check("git", {"operation": "rollback", "name": "c"}).reason
        assert smart.check("git", {"operation": "commit", "message": "x"}).reason == ""
        # classify_access 只读归类
        assert default.classify_access("git", {"operation": "diff"}) == "read"
        assert default.classify_access("git", {"operation": "commit"}) == "write"


# ═══════════════════════════════════════════════════════════
#  GitTool
# ═══════════════════════════════════════════════════════════

def _init_git_repo(path):
    import subprocess
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=path, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, capture_output=True, text=True)


class TestGitTool:
    def setup_method(self):
        from tools.git import GitTool
        self.tool = GitTool()

    @pytest.mark.asyncio
    async def test_non_git_repo_errors(self, tmp_path):
        self.tool.workspace = str(tmp_path)
        data = json.loads(await self.tool.execute(operation="status"))
        assert data["success"] is False
        assert "Git" in data["error"]

    @pytest.mark.asyncio
    async def test_status_and_commit(self, tmp_path):
        if not __import__("shutil").which("git"):
            pytest.skip("git not installed")
        _init_git_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("hello\n")
        self.tool.workspace = str(tmp_path)

        status = json.loads(await self.tool.execute(operation="status"))
        assert status["success"] is True

        commit = json.loads(await self.tool.execute(operation="commit", message="feat: add a"))
        assert commit["success"] is True
        assert commit["commit"]

        log = json.loads(await self.tool.execute(operation="log"))
        assert log["success"] is True
        assert "feat: add a" in log["output"]


# ═══════════════════════════════════════════════════════════
#  UsageTracker (existing)
# ═══════════════════════════════════════════════════════════

class TestUsageTracker:
    def test_track_and_summary(self):
        from usage import UsageTracker
        tracker = UsageTracker()
        tracker.track("glm-5", {"prompt_tokens": 100, "completion_tokens": 50})
        tracker.track("glm-5", {"prompt_tokens": 200, "completion_tokens": 100})

        summary = tracker.get_summary()
        assert summary["total_calls"] == 2
        assert summary["total_prompt_tokens"] == 300
        assert summary["total_completion_tokens"] == 150

    def test_reset(self):
        from usage import UsageTracker
        tracker = UsageTracker()
        tracker.track("glm-5", {"prompt_tokens": 100, "completion_tokens": 50})
        tracker.reset()
        assert tracker.get_summary()["total_calls"] == 0


# ═══════════════════════════════════════════════════════════
#  HookManager (existing)
# ═══════════════════════════════════════════════════════════

class TestHookManager:
    @pytest.mark.asyncio
    async def test_fire_hooks(self):
        from hooks import HookEvent, HookManager
        manager = HookManager()
        events = []

        async def hook1(ctx):
            events.append(f"hook1:{ctx.tool_name}")

        async def hook2(ctx):
            events.append(f"hook2:{ctx.tool_name}")

        manager.register(HookEvent.PRE_TOOL_USE, hook1)
        manager.register(HookEvent.PRE_TOOL_USE, hook2)

        await manager.fire(HookEvent.PRE_TOOL_USE, tool_name="test_tool")
        assert len(events) == 2
        assert "hook1:test_tool" in events
        assert "hook2:test_tool" in events

    @pytest.mark.asyncio
    async def test_hook_error_does_not_break(self):
        from hooks import HookEvent, HookManager
        manager = HookManager()

        async def bad_hook(ctx):
            raise ValueError("hook error")

        async def good_hook(ctx):
            pass

        manager.register(HookEvent.PRE_TOOL_USE, bad_hook)
        manager.register(HookEvent.PRE_TOOL_USE, good_hook)

        await manager.fire("pre_tool_use", tool_name="test")


# ═══════════════════════════════════════════════════════════
#  ApplyPatchTool
# ═══════════════════════════════════════════════════════════

class TestApplyPatchTool:
    def setup_method(self):
        from tools.apply_patch import ApplyPatchTool
        self.tool = ApplyPatchTool()

    @pytest.mark.asyncio
    async def test_apply_single_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
        self.tool.workspace = str(tmp_path)
        patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"

        r = await self.tool.execute(patch=patch)
        data = json.loads(r)
        assert data["success"] is True
        assert (tmp_path / "a.txt").read_text() == "one\nTWO\nthree\n"

    @pytest.mark.asyncio
    async def test_context_mismatch_rejects(self, tmp_path):
        (tmp_path / "b.txt").write_text("hello\n")
        self.tool.workspace = str(tmp_path)
        patch = "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,2 @@\n nope\n+added\n"

        r = await self.tool.execute(patch=patch)
        data = json.loads(r)
        assert data["success"] is False
        assert (tmp_path / "b.txt").read_text() == "hello\n"

    @pytest.mark.asyncio
    async def test_multifile_atomic(self, tmp_path):
        (tmp_path / "ok.txt").write_text("keep\nfoo()\n")
        (tmp_path / "bad.txt").write_text("other\n")
        self.tool.workspace = str(tmp_path)
        patch = (
            "--- a/ok.txt\n+++ b/ok.txt\n@@ -1,2 +1,2 @@\n keep\n-foo()\n+bar()\n"
            "--- a/bad.txt\n+++ b/bad.txt\n@@ -1,1 +1,2 @@\n WRONG\n+added\n"
        )

        r = await self.tool.execute(patch=patch)
        data = json.loads(r)
        assert data["success"] is False
        assert (tmp_path / "ok.txt").read_text() == "keep\nfoo()\n"
        assert (tmp_path / "bad.txt").read_text() == "other\n"

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, tmp_path):
        (tmp_path / "c.txt").write_text("x\ny\n")
        self.tool.workspace = str(tmp_path)
        patch = "--- a/c.txt\n+++ b/c.txt\n@@ -1,2 +1,2 @@\n x\n-y\n+Y\n"

        r = await self.tool.execute(patch=patch, dry_run=True)
        data = json.loads(r)
        assert data["success"] is True
        assert data["dry_run"] is True
        assert (tmp_path / "c.txt").read_text() == "x\ny\n"


# ═══════════════════════════════════════════════════════════
#  CodeDiagnosticsTool
# ═══════════════════════════════════════════════════════════

class TestDiagnostics:
    def test_detect_languages(self, tmp_path):
        from tools.diagnostics import detect_languages
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "package.json").write_text("{}\n")
        assert detect_languages(str(tmp_path)) == ["python", "node"]

    def test_detect_languages_empty(self, tmp_path):
        from tools.diagnostics import detect_languages
        assert detect_languages(str(tmp_path)) == []

    def test_parse_ruff(self):
        from tools.diagnostics import _parse_ruff
        payload = json.dumps([{
            "filename": "a.py", "code": "F401", "message": "unused import",
            "location": {"row": 3, "column": 1},
        }])
        issues = _parse_ruff(payload)
        assert issues[0]["file"] == "a.py"
        assert issues[0]["line"] == 3
        assert issues[0]["severity"] == "error"
        assert issues[0]["code"] == "F401"

    def test_parse_mypy(self):
        from tools.diagnostics import _parse_mypy
        issues = _parse_mypy("a.py:4:2: error: Incompatible types  [assignment]")
        assert issues[0]["line"] == 4 and issues[0]["severity"] == "error"

    def test_parse_tsc(self):
        from tools.diagnostics import _parse_tsc
        issues = _parse_tsc("src/a.ts(10,5): error TS2322: Type 'x' is not assignable")
        assert issues[0]["file"] == "src/a.ts" and issues[0]["line"] == 10

    def test_format_advisory(self):
        from tools.diagnostics import format_advisory
        result = {"results": [{"tool": "ruff", "status": "issues", "issue_count": 1,
                               "issues": [{"file": "a.py", "line": 1, "col": 1,
                                           "severity": "error", "code": "F401", "message": "unused"}]}]}
        text = format_advisory(result)
        assert "ruff" in text and "a.py:1:1" in text
        assert format_advisory({"results": [{"tool": "ruff", "status": "ok", "issues": []}]}) == ""

    @pytest.mark.asyncio
    async def test_tool_returns_structured_result(self, tmp_path):
        import importlib.util
        if importlib.util.find_spec("ruff") is None:
            pytest.skip("ruff not installed")
        from tools.diagnostics import CodeDiagnosticsTool
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
        (tmp_path / "bad.py").write_text("import os\n")
        tool = CodeDiagnosticsTool()
        tool.workspace = str(tmp_path)

        r = await tool.execute(path="bad.py")
        data = json.loads(r)
        assert data["success"] is True
        ruff = next(res for res in data["results"] if res["tool"] == "ruff")
        assert ruff["status"] in ("issues", "ok")
        if ruff["status"] == "issues":
            assert any(i["code"] == "F401" for i in ruff["issues"])
