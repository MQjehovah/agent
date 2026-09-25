import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  agentToExportSession,
  buildExportJson,
  buildExportMarkdown,
  localToExportSession,
  sanitizeExportFileName,
  type ExportSession
} from '../../electron/main/kernel/export'

/**
 * 会话导出(D2)纯逻辑: Markdown 组装 / JSON 序列化 / 文件名消毒 / 本地与在线消息归一化。
 */

const session: ExportSession = {
  title: '测试会话',
  mode: 'local',
  createdAt: Date.UTC(2023, 10, 14, 22, 13, 20),
  updatedAt: Date.UTC(2023, 10, 14, 22, 14, 20),
  messages: [
    { role: 'user', content: '你好' },
    { role: 'assistant', content: '', toolCalls: [{ name: 'read_file', arguments: '{"path":"a.txt"}' }] },
    { role: 'tool', content: '文件内容', name: 'read_file' },
    { role: 'assistant', content: '完成' }
  ]
}

test('export: Markdown 按轮次组装(标题/时间/用户/专家/工具调用摘要)', () => {
  const md = buildExportMarkdown(session)
  assert.equal(
    md,
    [
      '# 测试会话',
      '',
      '- 模式：本地',
      '- 创建时间：2023-11-14 22:13:20 UTC',
      '- 更新时间：2023-11-14 22:14:20 UTC',
      '',
      '## 用户',
      '',
      '你好',
      '',
      '## 专家',
      '',
      '（无正文）',
      '',
      '### 工具调用',
      '',
      '- `read_file`：{"path":"a.txt"}',
      '- 结果（read_file）：文件内容',
      '',
      '## 专家',
      '',
      '完成',
      ''
    ].join('\n')
  )
})

test('export: Markdown 在线会话与长文本/缺字段容错', () => {
  const longArgs = `{"q":"${'x'.repeat(400)}"}`
  const md = buildExportMarkdown({
    title: '',
    mode: 'agent',
    createdAt: 0,
    updatedAt: Date.UTC(2023, 10, 14, 22, 14, 20),
    messages: [
      { role: 'user', content: '搜一下' },
      { role: 'assistant', content: '', toolCalls: [{ name: 'search', arguments: longArgs }] },
      { role: 'tool', content: '结果' }
    ]
  })
  assert.ok(md.startsWith('# 会话\n'))
  assert.ok(md.includes('- 模式：在线'))
  assert.ok(md.includes('- 创建时间：-'))
  // 工具参数摘要单行截断到 200 字 + 省略号
  const argLine = md.split('\n').find((line) => line.startsWith('- `search`：'))!
  assert.equal(argLine.length, '- `search`：'.length + 201)
  assert.ok(argLine.endsWith('…'))
  // 无工具名的结果行不拼括号
  assert.ok(md.includes('- 结果：结果'))
})

test('export: JSON 结构与消息序列化', () => {
  const parsed = JSON.parse(buildExportJson(session)) as ExportSession
  assert.equal(parsed.title, '测试会话')
  assert.equal(parsed.mode, 'local')
  assert.equal(parsed.createdAt, session.createdAt)
  assert.equal(parsed.updatedAt, session.updatedAt)
  assert.deepEqual(parsed.messages, session.messages)
  assert.ok(buildExportJson(session).endsWith('\n'))
})

test('export: 文件名消毒(Windows 非法字符/空白/点/超长/空回退)', () => {
  assert.equal(sanitizeExportFileName('a/b\\c:d*e?f"g<h>i|j'), 'a b c d e f g h i j')
  assert.equal(sanitizeExportFileName('  季度 汇报  '), '季度 汇报')
  assert.equal(sanitizeExportFileName('会话\u0000\u001f名'), '会话 名')
  assert.equal(sanitizeExportFileName(''), '会话')
  assert.equal(sanitizeExportFileName('   '), '会话')
  assert.equal(sanitizeExportFileName('..'), '会话')
  assert.equal(sanitizeExportFileName('session.'), 'session')
  assert.equal(sanitizeExportFileName('a'.repeat(80)).length, 60)
})

test('export: 本地会话归一化(StoredMessage → ExportSession)', () => {
  const out = localToExportSession(
    {
      id: 'local-00000001',
      mode: 'local',
      title: 'T',
      model: 'm',
      workspace: 'w',
      createdAt: 1,
      updatedAt: 2
    },
    [
      { role: 'user', content: 'q', ts: 3 },
      {
        role: 'assistant',
        content: '',
        toolCalls: [{ id: 'c1', name: 'read_file', arguments: '{}' }],
        ts: 4
      }
    ]
  )
  assert.deepEqual(out, {
    title: 'T',
    mode: 'local',
    createdAt: 1,
    updatedAt: 2,
    messages: [
      { role: 'user', content: 'q', ts: 3 },
      { role: 'assistant', content: '', ts: 4, toolCalls: [{ name: 'read_file', arguments: '{}' }] }
    ]
  })
})

test('export: 在线会话归一化(snake_case → 归一形状, 标题回退 id)', () => {
  const out = agentToExportSession(
    {
      id: 'web:42:abcd',
      title: '',
      first_accessed: '2023-11-14T22:13:20.000Z',
      last_accessed: '2023-11-14T22:14:20.000Z'
    },
    [
      { role: 'user', content: '搜一下' },
      {
        role: 'assistant',
        content: '',
        reasoning_content: '思考',
        tool_calls: [{ id: 'c9', function: { name: 'search', arguments: '{"q":"1"}' } }]
      },
      { role: 'tool', content: 'ok', tool_call_id: 'c9', name: 'search' }
    ]
  )
  assert.equal(out.mode, 'agent')
  assert.equal(out.title, '会话 web:42:abcd')
  assert.equal(out.createdAt, Date.parse('2023-11-14T22:13:20.000Z'))
  assert.equal(out.updatedAt, Date.parse('2023-11-14T22:14:20.000Z'))
  assert.deepEqual(out.messages[0], { role: 'user', content: '搜一下' })
  assert.deepEqual(out.messages[1], {
    role: 'assistant',
    content: '',
    reasoning: '思考',
    toolCalls: [{ name: 'search', arguments: '{"q":"1"}' }]
  })
  assert.deepEqual(out.messages[2], { role: 'tool', content: 'ok', name: 'search', toolCallId: 'c9' })
})
