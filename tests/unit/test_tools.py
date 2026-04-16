"""工具单元测试"""
import os
import json
import pytest

# 设置 src 路径
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


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
            old_text="hello world",
            new_text="hello python"
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
            old_text="aaa",
            new_text="bbb",
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
            old_text="nonexistent",
            new_text="replacement"
        )
        assert json.loads(r)["success"] is False

    @pytest.mark.asyncio
    async def test_ambiguous_match(self, tmp_path):
        test_file = tmp_path / "ambiguous.txt"
        test_file.write_text("aaa\naaa\n")

        r = await self.tool.execute(
            path=str(test_file),
            old_text="aaa",
            new_text="bbb"
        )
        data = json.loads(r)
        assert data["success"] is False
        assert "2 处匹配" in data["error"]


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


class TestPermissionChecker:
    def setup_method(self):
        from permissions import PermissionChecker, PermissionConfig, PermissionMode
        self.auto_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.AUTO))
        self.default_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.DEFAULT))
        self.plan_checker = PermissionChecker(PermissionConfig(mode=PermissionMode.PLAN))

    def test_auto_mode_allows_all(self):
        result = self.auto_checker.check("shell", {"command": "rm -rf /"})
        assert result.allowed is True

    def test_plan_mode_blocks_writes(self):
        result = self.plan_checker.check("file_operation", {"operation": "write", "path": "/tmp/test"})
        assert result.allowed is False

    def test_plan_mode_allows_reads(self):
        result = self.plan_checker.check("file_operation", {"operation": "read", "path": "/tmp/test"})
        assert result.allowed is True

    def test_default_mode_needs_confirm_for_writes(self):
        result = self.default_checker.check("file_operation", {"operation": "write", "path": "/tmp/test"})
        assert result.allowed is True
        assert "确认" in result.reason

    def test_default_mode_allows_reads(self):
        result = self.default_checker.check("file_operation", {"operation": "read", "path": "/tmp/test"})
        assert result.allowed is True
        assert result.reason == ""

    def test_default_mode_allows_read_commands(self):
        result = self.default_checker.check("shell", {"command": "ls -la"})
        assert result.allowed is True
        assert result.reason == ""


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


class TestHookManager:
    @pytest.mark.asyncio
    async def test_fire_hooks(self):
        from hooks import HookManager, HookEvent
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
        from hooks import HookManager, HookEvent
        manager = HookManager()

        async def bad_hook(ctx):
            raise ValueError("hook error")

        async def good_hook(ctx):
            pass

        manager.register(HookEvent.PRE_TOOL_USE, bad_hook)
        manager.register(HookEvent.PRE_TOOL_USE, good_hook)

        # 不应抛出异常
        await manager.fire("pre_tool_use", tool_name="test")
