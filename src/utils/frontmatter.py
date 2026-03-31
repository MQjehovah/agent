"""
Frontmatter 解析工具模块

用于解析 Markdown 文件中的 YAML frontmatter
"""
import re
import logging
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("agent.utils")

# YAML frontmatter 格式正则
FRONTMATTER_PATTERN = r'^---\s*\n(.*?)\n---\s*\n(.*)$'


def extract_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """
    从 Markdown 内容中提取 YAML frontmatter 和正文

    Args:
        content: Markdown 文件内容

    Returns:
        Tuple[frontmatter_dict, body_text]
        - frontmatter_dict: 解析后的 frontmatter 字典，如果不存在则为空字典
        - body_text: 正文内容（去除 frontmatter 部分）
    """
    match = re.match(FRONTMATTER_PATTERN, content, re.DOTALL)

    if not match:
        return {}, content

    frontmatter_str = match.group(1)
    body = match.group(2).strip()

    frontmatter = _parse_yaml_frontmatter(frontmatter_str)

    return frontmatter, body


def _parse_yaml_frontmatter(frontmatter_str: str) -> Dict[str, Any]:
    """
    解析 YAML frontmatter 字符串

    Args:
        frontmatter_str: YAML frontmatter 字符串

    Returns:
        解析后的字典
    """
    import yaml

    try:
        frontmatter = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError as e:
        logger.error(f"YAML 解析错误: {e}")
        # 尝试简化解析
        frontmatter = _simple_parse(frontmatter_str)

    return frontmatter


def _simple_parse(frontmatter_str: str) -> Dict[str, Any]:
    """
    简化的 YAML 解析（不依赖 yaml 库）

    Args:
        frontmatter_str: YAML frontmatter 字符串

    Returns:
        解析后的字典
    """
    frontmatter = {}

    for line in frontmatter_str.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()

            # 数组类型
            if value.startswith('[') and value.endswith(']'):
                items = [item.strip().strip('"\'') for item in value[1:-1].split(',')]
                frontmatter[key] = [item for item in items if item]
            # 布尔类型
            elif value.lower() == 'true':
                frontmatter[key] = True
            elif value.lower() == 'false':
                frontmatter[key] = False
            # 字符串类型
            else:
                frontmatter[key] = value.strip('"\'')

    return frontmatter


def validate_required_fields(frontmatter: Dict[str, Any], required_fields: list) -> Tuple[bool, list]:
    """
    验证 frontmatter 是否包含必要字段

    Args:
        frontmatter: frontmatter 字典
        required_fields: 必须存在的字段列表

    Returns:
        Tuple[is_valid, missing_fields]
    """
    missing = [field for field in required_fields if field not in frontmatter]
    return len(missing) == 0, missing