"""remote_terminal 输出清洗：\\r 换行污染与命令回显过滤。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

from remote_terminal import ANSIStripper, TerminalParser  # noqa: E402


CMD = (
    "grep -n -i 'lost\\|relocalize\\|timeout\\|crash' "
    "/opt/xzrobot/logs/xzrobot_navigaiton/info_20260806-164614.2800 "
    "2>/dev/null | head -5"
)


def test_cr_overwrite_does_not_invent_loggs():
    raw = "grep /opt/xzrobot/log\rgs/xzrobot_navigaiton/foo"
    cleaned = ANSIStripper.clean_for_display(raw)
    assert "loggs" not in cleaned
    assert "logs" not in cleaned or cleaned.startswith("gs/") or "gs/" in cleaned


def test_cr_timestamp_does_not_duplicate_digit():
    raw = "ls -l /opt/xzrobot/logs/xzrobot_navigaiton/ | grep '20260806-1\r164614'\r\n"
    cleaned = ANSIStripper.clean_for_display(raw)
    assert "20260806-1164614" not in cleaned


def test_backspace_applies():
    assert ANSIStripper.clean_for_display("ab\x08c") == "ac"


def test_parse_drops_garbled_echo_keeps_hits():
    parser = TerminalParser()
    # 旧清洗会得到 loggs；现清洗为覆盖后的残段，均应被回显过滤丢掉
    raw_echo = (
        "grep -n -i 'lost\\|relocalize\\|timeout\\|crash' "
        "/opt/xzrobot/log\rgs/xzrobot_navigaiton/info_20260806-164614.2800 "
        "2>/dev/null | head -5\r\n"
    )
    hit = "12:[2026-08-06 16:46:20.001][error] relocate timeout\r\n"
    result = parser.parse_command_response([raw_echo, hit], CMD)
    assert "loggs" not in result.output
    assert "relocate timeout" in result.output
    assert result.output.startswith("12:")


def test_parse_empty_when_only_garbled_echo():
    parser = TerminalParser()
    raw = (
        "grep -n -i 'lost\\|relocalize\\|timeout\\|crash' "
        "/opt/xzrobot/log\rgs/xzrobot_navigaiton/info_20260806-164614.2800 "
        "2>/dev/null | head -5\r\n"
    )
    result = parser.parse_command_response([raw], CMD)
    assert result.output == ""
    assert "loggs" not in result.output


def test_parse_keeps_real_grep_lines_with_path_like_text():
    parser = TerminalParser()
    hit = (
        "2:[2026-08-06 09:14:01.939][info] [/xzrobot_chassis] "
        "path=/opt/xzrobot/logs/xzrobot_driver2 fall_mask=false\n"
    )
    result = parser.parse_command_response([hit], CMD)
    assert "fall_mask=false" in result.output
    assert result.output.startswith("2:")


def test_default_cols_wide():
    from remote_terminal import DEFAULT_COLS, DEFAULT_ROWS

    assert DEFAULT_COLS >= 200
    assert DEFAULT_ROWS >= 24
