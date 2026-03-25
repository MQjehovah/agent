import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("agent.memory")


class MemoryExtractor:
    def __init__(self, llm_client=None):
        self.llm_client = llm_client
    
    def extract_to_daily(self, session_content: str, daily_file: str) -> bool:
        if not self.llm_client:
            return self._simple_extract(session_content, daily_file)
        
        return self._llm_extract(session_content, daily_file)
    
    def _simple_extract(self, session_content: str, daily_file: str) -> bool:
        new_sections = self._extract_sections(session_content)
        if not new_sections:
            return False
        
        date_str = datetime.now().strftime("%Y-%m-%d")
        header = f"# 每日记忆 - {date_str}\n\n"
        content = header + "\n".join(new_sections)
        
        with open(daily_file, "w", encoding="utf-8") as f:
            f.write(content)
        
        logger.info(f"Daily memory updated: {daily_file}")
        return True
    
    def _extract_sections(self, content: str) -> list:
        sections = []
        lines = content.split("\n")
        current_section = []
        section_title = ""
        
        for line in lines:
            if line.startswith("## "):
                if current_section and section_title:
                    sections.append("\n".join(current_section))
                section_title = line
                current_section = [line]
            elif current_section:
                current_section.append(line)
        
        if current_section and section_title:
            sections.append("\n".join(current_section))
        
        return sections
    
    def _llm_extract(self, session_content: str, daily_file: str) -> bool:
        existing_content = ""
        if os.path.exists(daily_file):
            with open(daily_file, "r", encoding="utf-8") as f:
                existing_content = f.read()
            if existing_content.startswith("# "):
                lines = existing_content.split("\n", 2)
                if len(lines) >= 3:
                    existing_content = lines[2]
        
        if existing_content.strip():
            prompt = f"""你是一个记忆管理助手。请将新的会话信息合并到已有的每日记忆中，生成一份整合后的每日记忆。

要求：
1. 去重：相同或相似的信息只保留一条
2. 归类：将信息放入合适的分类（业务洞察、用户偏好、学到的知识）
3. 简洁：保留关键信息，去除冗余

已有每日记忆：
{existing_content}

新会话记录：
{session_content}

请输出整合后的每日记忆，格式如下：

## 业务洞察
- [重要业务发现]

## 用户偏好
- [用户偏好信息]

## 学到的知识
- [新学到的知识或流程]

只输出内容，不要添加额外说明。"""
        else:
            prompt = f"""请从以下会话记录中提取关键信息，按以下格式整理：

## 业务洞察
- [重要业务发现]

## 用户偏好
- [用户偏好信息]

## 学到的知识
- [新学到的知识或流程]

会话记录：
{session_content}

请只输出提取的内容，不要添加额外说明。"""

        try:
            response = self.llm_client.chat(
                [{"role": "user", "content": prompt}],
                [],
                stream=False
            )
            extracted = response.choices[0].message.content
            
            date_str = datetime.now().strftime("%Y-%m-%d")
            content = f"# 每日记忆 - {date_str}\n\n{extracted}"
            
            with open(daily_file, "w", encoding="utf-8") as f:
                f.write(content)
            
            logger.info(f"Daily memory extracted with LLM: {daily_file}")
            return True
        except Exception as e:
            logger.error(f"LLM extraction failed: {e}")
            return self._simple_extract(session_content, daily_file)