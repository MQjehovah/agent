import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  createTaskTool,
  listTeamMembers,
  parseAgentMarkdown,
  readMemberPrompt,
  type TeamMember
} from '../../electron/main/kernel/subagent'

test('subagent: parseAgentMarkdown 解析 frontmatter 与正文', () => {
  const raw = '---\nname: coder\ndescription: "写代码"\n---\n你是工程师'
  assert.deepEqual(parseAgentMarkdown(raw, 'fallback'), { name: 'coder', description: '写代码', body: '你是工程师' })
  // 无 frontmatter：名称回退，正文为全文
  assert.deepEqual(parseAgentMarkdown('直接正文', 'fallback'), { name: 'fallback', description: '', body: '直接正文' })
})

test('subagent: listTeamMembers 跳过 leader 并读取描述，readMemberPrompt 取正文', (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'team-'))
  t.after(() => rmSync(dataDir, { recursive: true, force: true }))
  const dir = join(dataDir, 'expert')
  mkdirSync(join(dir, 'agents'), { recursive: true })
  writeFileSync(join(dir, 'TEAM.md'), '# 团队')
  writeFileSync(join(dir, 'agents', 'lead.md'), '---\ndescription: 队长\n---\n你是队长')
  writeFileSync(join(dir, 'agents', 'dev.md'), '---\ndescription: 开发\n---\n你是开发')
  writeFileSync(join(dir, 'agents', 'qa.md'), '---\ndescription: 测试\n---\n你是测试')

  assert.deepEqual(listTeamMembers(dir, 'lead'), [
    { name: 'dev', description: '开发' },
    { name: 'qa', description: '测试' }
  ])
  assert.equal(readMemberPrompt(dir, 'dev'), '你是开发')
  assert.equal(readMemberPrompt(dir, 'ghost'), '你是团队成员「ghost」。')
})

test('subagent: createTaskTool 校验成员/任务并委派 run', async () => {
  const members: TeamMember[] = [{ name: 'dev', description: '开发' }]
  const calls: Array<{ member: string; task: string }> = []
  const tool = createTaskTool({
    members,
    run: async (member, task) => {
      calls.push({ member, task })
      return { ok: true, output: `${member}:${task}` }
    }
  })

  const unknown = await tool.execute({ member: 'x', task: 't' }, { workspace: 'w', sessionId: 's' })
  assert.equal(unknown.ok, false)
  assert.ok(unknown.output.includes('未知成员'), unknown.output)

  const noTask = await tool.execute({ member: 'dev' }, { workspace: 'w', sessionId: 's' })
  assert.equal(noTask.ok, false)
  assert.ok(noTask.output.includes('task'), noTask.output)

  const ok = await tool.execute({ member: 'dev', task: '实现登录' }, { workspace: 'w', sessionId: 's' })
  assert.deepEqual(ok, { ok: true, output: 'dev:实现登录' })
  assert.deepEqual(calls, [{ member: 'dev', task: '实现登录' }])
  assert.ok(tool.description.includes('dev：开发'))
})
