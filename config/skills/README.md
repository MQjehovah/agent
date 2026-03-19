# Skills 技能系统

## 概述

采用Claude风格的文件夹技能系统，每个技能是一个独立目录，包含配置和提示词模板。

## 目录结构

```
skills/
├── data-analyst/           # 数据分析专家
│   ├── skill.json          # 技能定义
│   ├── prompt.md           # 提示词模板
│   └── examples/           # 示例（可选）
├── report-writer/          # 报告撰写助手
│   ├── skill.json
│   └── prompt.md
└── inventory-monitor/      # 库存监控专家
    ├── skill.json
    └── prompt.md
```

## 使用方法

```bash
# 列出所有技能
python agent.py --list-skills

# 执行技能
python agent.py -s data-analyst -t "分析本月销售数据"

# 交互模式
python agent.py
> skills
```

## 创建新技能

```bash
mkdir skills/my-skill
# 创建 skill.json 和 prompt.md
```

## 内置技能

| 技能名称 | 描述 | 工具 |
|---------|------|------|
| data-analyst | 数据分析专家 | execute_query, list_tables, describe_table |
| report-writer | 报告撰写助手 | send_email |
| inventory-monitor | 库存监控专家 | execute_query, send_email |
