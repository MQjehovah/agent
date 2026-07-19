"""
对抗性代码审查 — 以攻击者视角主动猎杀 Bug

设计思路（参考 grok-build 的 grok_challenge）：
- 不是"代码写得好不好"，而是"哪里可能出问题"
- 主动猎杀：Bug、竞态条件、边界情况、安全漏洞
- 结果分级：CRITICAL / HIGH / MEDIUM / LOW
- 每个发现附带复现步骤和修复建议

用法:
    verifier = AdversarialVerifier(client)
    findings = await verifier.challenge(code_context)
    # findings = [
    #   {"severity": "HIGH", "title": "...", "file": "...:42",
    #    "reproduction": "...", "patch": "..."},
    # ]
"""
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("agent.adversarial")


class AdversarialVerifier:
    """对抗性代码审查器"""

    def __init__(self, client=None):
        self.client = client
        self._findings: list[dict] = []

    async def challenge(self, code_context: str, focus: str = "all") -> list[dict]:
        """对代码进行对抗性审查

        Args:
            code_context: 代码文本或 diff
            focus: 审查焦点
                "all" - 全部
                "bugs" - 仅 Bug
                "security" - 仅安全
                "race" - 仅竞态条件
                "edge" - 仅边界情况

        Returns:
            按严重程度排序的问题列表
        """
        if not self.client:
            logger.warning("[adversarial] 无 LLM 客户端，使用规则审查")
            return self._rule_based_challenge(code_context)

        # 构建 prompt
        focus_instruction = self._get_focus_instruction(focus)

        prompt = f"""你是一个极有经验的安全研究员和代码审查专家。请以"攻击者/破坏者"视角审查以下代码。

你的目标是**找出所有可能出问题的地方**，包括但不限于：
- 隐藏的 Bug（不仅仅看明显错误，还要看出乎意料的输入/状态）
- 竞态条件（race condition, TOCTOU）
- 边界情况（空值、负数、超长输入、零值）
- 安全漏洞（注入、越权、信息泄露、SSRF）
- 异常处理缺失（什么情况下会 crash？）
- 逻辑矛盾（看似正确的代码在特定条件下反而错了）

{focus_instruction}

## 需要审查的代码
```
{code_context[:8000]}
```

## 输出格式
返回 JSON 数组，按严重程度排序（最严重的排最前）：
```json
[
  {{
    "severity": "CRITICAL|HIGH|MEDIUM|LOW",
    "title": "问题标题",
    "category": "bug|race_condition|security|edge_case|error_handling|logic",
    "location": "文件:行号（如已知）",
    "description": "问题的详细描述（为什么这是问题，什么条件下会触发）",
    "reproduction": "复现步骤（具体的输入或操作序列）",
    "impact": "如果被利用会有什么后果",
    "patch": "修复建议（具体的代码修改，如果适用）",
    "confidence": 0.0-1.0
  }}
]
```

## 审查原则
1. 严苛但不偏执：合理怀疑，但不要无中生有
2. 具体可操作：每个问题必须有明确的位置和复现条件
3. 按严重程度分级：
   - CRITICAL: 可导致数据泄露、远程代码执行、服务不可用
   - HIGH: 可导致功能失效、数据损坏、权限提升
   - MEDIUM: 可导致异常行为、性能问题
   - LOW: 代码异味、最佳实践偏离

只返回 JSON 数组。不要输出任何其他内容。"""

        try:
            resp = await self.client.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            findings = json.loads(content)
            if isinstance(findings, list):
                # 按 severity 排序
                self._sort_by_severity(findings)
                self._findings = findings
                return findings
        except Exception as e:
            logger.warning(f"[adversarial] LLM 审查失败，回退到规则审查: {e}")

        return self._rule_based_challenge(code_context)

    async def challenge_by_diff(self, diff_text: str) -> list[dict]:
        """专门审查 diff（变更的代码）"""
        if not diff_text:
            return []

        focus = """重点关注变更是否引入了新的问题：
- 新增的代码是否处理了所有边界情况？
- 删除的代码是否影响了其他模块？
- 变更是否考虑了并发安全性？
- 变更是否与现有逻辑一致？"""

        return await self.challenge(f"## Diff\n{diff_text[:8000]}", focus="all")

    def _get_focus_instruction(self, focus: str) -> str:
        instructions = {
            "all": "覆盖所有方面：Bug、竞态条件、边界情况、安全漏洞、异常处理、逻辑矛盾。",
            "bugs": "重点关注潜在的 Bug：逻辑错误、错误的变量使用、算数错误、类型错误。不要关注安全或风格问题。",
            "security": "重点关注安全漏洞：注入、越权、XSS、CSRF、SSRF、信息泄露、不安全的反序列化、缺少输入验证。",
            "race": "重点关注竞态条件和并发问题：TOCTOU、缺少锁、不一致的状态更新、死锁、活锁。",
            "edge": "重点关注边界情况：空值、负数、零、极大值、空字符串、特殊字符、并发访问。",
        }
        return instructions.get(focus, instructions["all"])

    def _rule_based_challenge(self, code_context: str) -> list[dict]:
        """无 LLM 时的规则兜底审查"""
        import re
        findings = []

        lines = code_context.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()

            # 检查裸露的 exec/eval
            if re.search(r'\b(eval|exec)\s*\(', stripped) and not stripped.startswith("#"):
                findings.append({
                    "severity": "HIGH",
                    "title": "使用危险的 eval/exec",
                    "category": "security",
                    "location": f"第 {i+1} 行",
                    "description": f"eval/exec 执行动态代码，可能导致远程代码执行",
                    "reproduction": f"如果 {stripped[:50]} 中包含用户可控的数据，攻击者可执行任意代码",
                    "impact": "远程代码执行",
                    "patch": "使用 ast.literal_eval() 替代 eval()，或避免执行动态代码",
                    "confidence": 0.8,
                })

            # 检查 SQL 拼接
            if re.search(r"execute\s*\([\"']SELECT.*f[\"']\s*%|\.format\(|f[\"']SELECT", stripped):
                findings.append({
                    "severity": "CRITICAL",
                    "title": "可能的 SQL 注入",
                    "category": "security",
                    "location": f"第 {i+1} 行",
                    "description": f"SQL 查询使用了字符串拼接而非参数化查询",
                    "reproduction": "在用户输入中包含 ' OR 1=1 -- 可绕过查询条件",
                    "impact": "SQL 注入，可能导致数据泄露",
                    "patch": "使用参数化查询: cursor.execute(sql, params)",
                    "confidence": 0.6,
                })

            # 检查硬编码密码/密钥
            if re.search(r'(password|secret|api_key|token)\s*=\s*["\'][^"\']+["\']', stripped, re.I):
                findings.append({
                    "severity": "HIGH",
                    "title": "硬编码密钥/密码",
                    "category": "security",
                    "location": f"第 {i+1} 行",
                    "description": f"疑似硬编码的凭证",
                    "reproduction": "代码被推送到公共仓库后，凭证泄露",
                    "impact": "凭据泄露，未授权访问",
                    "patch": "使用环境变量或密钥管理服务",
                    "confidence": 0.5,
                })

            # 检查 except: 裸异常
            if re.search(r'except\s*:', stripped) and not re.search(r'except\s+\w+', stripped):
                findings.append({
                    "severity": "MEDIUM",
                    "title": "裸 except 捕获所有异常",
                    "category": "error_handling",
                    "location": f"第 {i+1} 行",
                    "description": "裸 except 会捕获包括 SystemExit 和 KeyboardInterrupt 在内的所有异常",
                    "reproduction": "当程序需要正常退出时被裸 except 吞掉",
                    "impact": "可能隐藏关键错误，使调试困难",
                    "patch": "except Exception as e: 替代 except:",
                    "confidence": 0.9,
                })

            # 检查 print 调试语句（可能遗留）
            if re.search(r'^\s*print\(', stripped) and not stripped.startswith("#"):
                findings.append({
                    "severity": "LOW",
                    "title": "调试用的 print 语句",
                    "category": "bug",
                    "location": f"第 {i+1} 行",
                    "description": "代码中遗留了调试用的 print 语句",
                    "reproduction": "正常运行时会有意外输出",
                    "impact": "无安全影响，但影响整洁性",
                    "patch": "使用 logging 模块替代 print",
                    "confidence": 0.7,
                })

        self._sort_by_severity(findings)
        self._findings = findings
        return findings

    @staticmethod
    def _sort_by_severity(findings: list[dict]):
        """按严重程度排序"""
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        findings.sort(key=lambda f: order.get(f.get("severity", "LOW"), 99))

    def get_report(self) -> dict:
        """获取审查报告"""
        if not self._findings:
            return {"summary": "未发现问题", "total": 0, "findings": []}

        critical = sum(1 for f in self._findings if f.get("severity") == "CRITICAL")
        high = sum(1 for f in self._findings if f.get("severity") == "HIGH")
        medium = sum(1 for f in self._findings if f.get("severity") == "MEDIUM")
        low = sum(1 for f in self._findings if f.get("severity") == "LOW")

        return {
            "summary": f"发现 {len(self._findings)} 个问题: {critical} CRITICAL, {high} HIGH, {medium} MEDIUM, {low} LOW",
            "total": len(self._findings),
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
            "findings": self._findings,
        }
