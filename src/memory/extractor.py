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
        existing_content = ""
        if os.path.exists(daily_file):
            with open(daily_file, "r", encoding="utf-8") as f:
                existing_content = f.read()
        
        new_sections = self._extract_sections(session_content)
        if not new_sections:
            return False
        
        if not existing_content:
            date_str = datetime.now().strftime("%Y-%m-%d")
            header = f"# 每日记忆 - {date_str}\n\n"
            content = header + "\n".join(new_sections)
        else:
            content = self._merge_content(existing_content, new_sections)
        
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
    
    def _merge_content(self, existing: str, new_sections: list) -> str:
        lines = existing.split("\n")
        result = []
        current_section_lines = []
        
        section_map = {}
        for section in new_sections:
            sec_lines = section.split("\n")
            if sec_lines:
                title = sec_lines[0]
                section_map[title] = sec_lines[1:] if len(sec_lines) > 1 else []
        
        for line in lines:
            if line.startswith("## "):
                if current_section_lines:
                    result.extend(current_section_lines)
                current_section_lines = [line]
            elif current_section_lines:
                current_section_lines.append(line)
        
        for title, items in section_map.items():
            if title in existing:
                continue
            result.append("")
            result.append(title)
            result.extend(items)
        
        return "\n".join(result)
    
    def _llm_extract(self, session_content: str, daily_file: str) -> bool:
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
            if os.path.exists(daily_file):
                with open(daily_file, "r", encoding="utf-8") as f:
                    existing = f.read()
                content = existing + "\n\n" + extracted
            else:
                content = f"# 每日记忆 - {date_str}\n\n{extracted}"
            
            with open(daily_file, "w", encoding="utf-8") as f:
                f.write(content)
            
            logger.info(f"Daily memory extracted with LLM: {daily_file}")
            return True
        except Exception as e:
            logger.error(f"LLM extraction failed: {e}")
            return self._simple_extract(session_content, daily_file)