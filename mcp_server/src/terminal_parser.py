"""
终端数据流解析器

从 WebSocket 终端数据流中解析命令和响应
"""
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum


class OutputType(Enum):
    """输出类型"""
    COMMAND_ECHO = "command_echo"      # 命令回显（用户输入）
    COMMAND_OUTPUT = "command_output"  # 命令输出
    PROMPT = "prompt"                  # 提示符
    CONTROL = "control"                # 控制序列
    ERROR = "error"                    # 错误信息
    UNKNOWN = "unknown"                # 未知


@dataclass
class ParsedOutput:
    """解析后的输出"""
    type: OutputType
    content: str
    raw: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "content": self.content,
            "raw": self.raw,
            "timestamp": self.timestamp
        }


@dataclass
class CommandResult:
    """命令执行结果"""
    command: str
    output: str
    success: bool = True
    error: str = ""
    raw_output: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "output": self.output,
            "success": self.success,
            "error": self.error,
            "raw_output": self.raw_output
        }


class ANSIStripper:
    """ANSI 转义序列过滤器"""

    # ANSI 转义序列正则
    ANSI_PATTERN = re.compile(
        r'\x1B(?:'
        r'[\[(][0-9;]*[a-zA-Z]'  # CSI 序列: ESC[...字母
        r'|][0-9;]*[a-zA-Z]'     # OSC 序列
        r'|[()][AB012]'          # 字符集选择
        r'|[78]'                 # 保存/恢复光标
        r'|[DM]'                 # 删除行/移动光标
        r')|'
        r'\x07'                  # BEL
        r'|\x1B[=>]'             # 键盘模式
        r'|\r'                   # 回车
        r'|\x00'                 # 空字符
    )

    @classmethod
    def strip(cls, text: str) -> str:
        """移除 ANSI 转义序列"""
        return cls.ANSI_PATTERN.sub('', text)

    @classmethod
    def clean_for_display(cls, text: str) -> str:
        """清理文本用于显示"""
        # 移除 ANSI 序列
        text = cls.strip(text)
        # 移除控制字符
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        # 处理退格
        while '\x08' in text:
            text = text.replace('\x08', '')
        return text.strip()


class TerminalParser:
    """终端数据流解析器"""

    # 常见提示符模式
    PROMPT_PATTERNS = [
        r'^\[[\w@\-]+\][\w\$#]\s*$',           # [user@host]$
        r'^[\w\-]+@[\w\-]+:~?[\/\w]*[\$#]\s*$', # user@host:~$
        r'^[\w\-]+[\$#]\s*$',                   # user$
        r'^root@[\w\-]+:.*[\$#]\s*$',           # root@host:#
        r'^\$\s*$',                              # $
        r'^#\s*$',                               # #
        r'^>\s*$',                               # >
        r'^.*[@\$#]\s*$',                        # 通用模式
    ]

    # 命令行编辑字符
    EDIT_CHARS = {'\x7f', '\x08', '\x1b'}  # DEL, BS, ESC

    def __init__(self, prompt_pattern: str = None):
        """
        初始化解析器

        Args:
            prompt_pattern: 自定义提示符正则模式
        """
        self.prompt_pattern = prompt_pattern
        self.buffer: List[str] = []
        self.last_command: str = ""
        self.pending_output: List[str] = []
        self._last_output_time: float = 0

    def _is_prompt(self, text: str) -> bool:
        """检查文本是否为提示符"""
        clean = ANSIStripper.clean_for_display(text).strip()

        if self.prompt_pattern:
            return bool(re.match(self.prompt_pattern, clean))

        for pattern in self.PROMPT_PATTERNS:
            if re.match(pattern, clean, re.MULTILINE):
                return True

        return False

    def _extract_command_echo(self, output: str, command: str) -> Tuple[str, str]:
        """
        从输出中分离命令回显

        Returns:
            (command_echo, remaining_output)
        """
        clean_output = ANSIStripper.clean_for_display(output)
        clean_command = command.strip()

        # 查找命令回显
        lines = clean_output.split('\n')
        echo_lines = []
        remaining_lines = []

        found_echo = False
        for line in lines:
            clean_line = line.strip()
            if not found_echo and clean_command in clean_line:
                # 检查是否是命令回显（通常命令在行首）
                if clean_line.startswith(clean_command) or clean_command in clean_line:
                    echo_lines.append(line)
                    found_echo = True
                    continue
            remaining_lines.append(line)

        return '\n'.join(echo_lines), '\n'.join(remaining_lines)

    def parse_chunk(self, chunk: str, expect_command: str = None) -> List[ParsedOutput]:
        """
        解析单个数据块

        Args:
            chunk: 原始数据块
            expect_command: 期望的命令（用于识别回显）

        Returns:
            解析后的输出列表
        """
        results = []

        # 处理控制消息
        if chunk.startswith('{') and chunk.endswith('}'):
            try:
                import json
                msg = json.loads(chunk)
                results.append(ParsedOutput(
                    type=OutputType.CONTROL,
                    content=json.dumps(msg),
                    raw=chunk
                ))
                return results
            except json.JSONDecodeError:
                pass

        # 清理 ANSI 序列用于分析
        clean = ANSIStripper.clean_for_display(chunk)

        # 按行分割
        lines = chunk.split('\n')

        for line in lines:
            if not line.strip():
                continue

            clean_line = ANSIStripper.clean_for_display(line)

            # 检查是否为提示符
            if self._is_prompt(clean_line):
                results.append(ParsedOutput(
                    type=OutputType.PROMPT,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 检查是否为命令回显
            elif expect_command and expect_command.strip() in clean_line:
                results.append(ParsedOutput(
                    type=OutputType.COMMAND_ECHO,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 检查是否为错误
            elif any(kw in clean_line.lower() for kw in ['error', 'failed', 'not found', 'permission denied', 'no such']):
                results.append(ParsedOutput(
                    type=OutputType.ERROR,
                    content=clean_line.strip(),
                    raw=line
                ))
            # 普通输出
            else:
                results.append(ParsedOutput(
                    type=OutputType.COMMAND_OUTPUT,
                    content=clean_line.strip(),
                    raw=line
                ))

        return results

    def parse_command_response(self, outputs: List[str], command: str) -> CommandResult:
        """
        解析完整的命令响应

        Args:
            outputs: 原始输出列表
            command: 执行的命令

        Returns:
            命令执行结果
        """
        # 合并所有输出
        full_output = ''.join(outputs)

        # 清理输出
        clean_output = ANSIStripper.clean_for_display(full_output)

        # 分行处理
        lines = clean_output.split('\n')

        # 过滤并分类
        result_lines = []
        prompt_found = False
        has_error = False
        error_msg = ""

        for line in lines:
            stripped = line.strip()

            # 跳过空行
            if not stripped:
                continue

            # 检查是否为提示符
            if self._is_prompt(stripped):
                prompt_found = True
                continue

            # 检查是否为命令回显
            if command.strip() in stripped and stripped.startswith(command.strip().split()[0]):
                continue

            # 检查错误
            if any(kw in stripped.lower() for kw in ['error:', 'failed:', 'not found', 'permission denied', 'no such file']):
                has_error = True
                error_msg = stripped

            result_lines.append(stripped)

        return CommandResult(
            command=command,
            output='\n'.join(result_lines),
            success=not has_error,
            error=error_msg,
            raw_output=outputs
        )


class InteractiveTerminalSession:
    """
    交互式终端会话管理器

    维护会话状态，支持命令-响应配对
    """

    def __init__(self, prompt_pattern: str = None):
        self.parser = TerminalParser(prompt_pattern)
        self.command_history: List[Dict[str, Any]] = []
        self.output_buffer: List[str] = []
        self._current_command: str = ""
        self._command_sent_time: float = 0

    def start_command(self, command: str):
        """
        记录开始执行命令

        Args:
            command: 要执行的命令
        """
        self._current_command = command
        self._command_sent_time = time.time()
        self.output_buffer = []

    def collect_output(self, output: str):
        """
        收集命令输出

        Args:
            output: 输出数据
        """
        self.output_buffer.append(output)

    def finish_command(self, timeout: float = 0.5) -> CommandResult:
        """
        完成命令执行，解析结果

        Args:
            timeout: 等待额外输出的超时时间

        Returns:
            命令执行结果
        """
        result = self.parser.parse_command_response(
            self.output_buffer,
            self._current_command
        )

        # 记录历史
        self.command_history.append({
            "command": self._current_command,
            "output": result.output,
            "success": result.success,
            "timestamp": self._command_sent_time
        })

        # 重置状态
        self._current_command = ""
        self.output_buffer = []

        return result

    def get_last_command_result(self) -> Optional[CommandResult]:
        """获取最后一条命令的结果"""
        if not self.command_history:
            return None

        last = self.command_history[-1]
        return CommandResult(
            command=last["command"],
            output=last["output"],
            success=last["success"]
        )

    def parse_streaming_output(self, chunk: str) -> List[ParsedOutput]:
        """
        解析流式输出

        Args:
            chunk: 数据块

        Returns:
            解析结果列表
        """
        return self.parser.parse_chunk(chunk, self._current_command)


def parse_terminal_output(
    outputs: List[str],
    command: str = None,
    strip_ansi: bool = True
) -> Dict[str, Any]:
    """
    便捷函数：解析终端输出

    Args:
        outputs: 原始输出列表
        command: 执行的命令（可选）
        strip_ansi: 是否移除 ANSI 序列

    Returns:
        解析结果字典
    """
    parser = TerminalParser()

    if command:
        result = parser.parse_command_response(outputs, command)
        return result.to_dict()

    # 没有命令信息，只做基本解析
    all_parsed = []
    for output in outputs:
        parsed = parser.parse_chunk(output)
        all_parsed.extend([p.to_dict() for p in parsed])

    return {
        "parsed": all_parsed,
        "raw": outputs
    }