---
name: doubt-driven-development
description: Adversarial fresh-context review of every non-trivial decision. Use when stakes are high (production, security, irreversible).
---

# 怀疑驱动开发

## Overview
人类（和 AI）天然倾向于确认偏差——找到支持自己已有结论的证据，忽略反对的证据。怀疑驱动开发是这种偏差的系统性解药：对每一个非平凡的决策，用新鲜的视角对抗性地审视它。如果经得起怀疑，它可能是对的。如果经不起，庆幸你提前发现了。

## When to Use
- 生产环境部署前 review 关键变更
- 涉及安全、权限、数据完整性的决策
- 不可逆操作（数据迁移、删除操作、外部发布）
- 写测试时，用来检查测试是否真的在测正确的事情
- 代码审查时
- NOT for 每一个变量命名决策（成本 > 收益）
- NOT for 已经在其他地方做过完整 review 的简单变更

## Core Process

1. **CLAIM** — 明确写出你要质疑的决策或结论。"我认为 X 方案是最优的，因为 Y"。
2. **EXTRACT** — 提取背后的假设。列出所有支撑这个结论的未经验证的前提。
3. **DOUBT** — 对每个假设提出对抗性证据。尝试证伪而非证实。问："什么情况下这个假设会不成立？"
4. **RECONCILE** — 评估质疑的结果。假设是否仍然成立？需要修改方案吗？
5. **STOP** — 设定质疑时间盒（建议 < 15 分钟）。不要陷入无限怀疑的瘫痪状态。

### Fresh-Context Review
- 切换上下文后再 review 自己的代码（做半小时别的事再回来看）
- 让别人 review 时，不提供背景解释（让他们自己读代码理解）
- 对 AI Agent：清除对话历史后重新提需求，看输出是否一致

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "这个方案我仔细想过了，没问题" | 仔细想过不等于没有盲点 |
| "大家都这么做" | 从众是确认偏差的集体形态 |
| "我们一直这样做的" | 历史正确性不能推导未来正确性 |
| "时间不够了，先上线再说" | 上线后发现错了的时间成本更高 |
| "这个测试通过了，代码就是对的" | 测试可能测了错误的事情，或者根本没测 |

## Red Flags
- 听到自己说"肯定是 X 的问题"（确定性语言是危险信号）
- 决策过程缺乏任何逆向思考的痕迹
- 无法写出 CLAIM 步骤中的 "因为 Y" 部分
- review 时只关注了实现细节，没质疑方案本身
- "不用测了，肯定没问题"
- 拒绝让别人 review 代码

## Verification
- [ ] 关键决策已用 CLAIM → EXTRACT → DOUBT → RECONCILE 流程审视
- [ ] 决策的所有关键假设已列出并质疑过
- [ ] 有明确的质疑时间盒，没有陷入无限怀疑
- [ ] 测试通过了，但我还质疑了"测试是否测了对的东西"
- [ ] 高风险的不可逆操作前做了 fresh-context review
