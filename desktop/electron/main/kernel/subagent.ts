import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import type { ToolDefinition } from './types'

/**
 * 团队专家(专家插件内 agents/*.md 成员)的解析与编排。
 * - 成员提示词/描述来自标准 markdown(frontmatter name/description，正文即提示词)
 * - task 工具：模型按成员名派活，run 注入真实子代理执行(见 ipc 的 runTeamMember)
 */

export interface TeamMember {
  name: string
  description: string
}

/** 解析 markdown frontmatter 的 name/description，正文(闭合围栏之后)为提示词 */
export function parseAgentMarkdown(
  raw: string,
  fallbackName: string
): { name: string; description: string; body: string } {
  const lines = raw.replace(/^\uFEFF/, '').split(/\r?\n/)
  let name = fallbackName
  let description = ''
  let bodyStart = 0
  if (lines[0]?.trim() === '---') {
    for (let i = 1; i < lines.length; i++) {
      if (lines[i].trim() === '---') {
        bodyStart = i + 1
        break
      }
      const m = lines[i].match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/)
      if (!m) continue
      const value = m[2].trim().replace(/^["']|["']$/g, '')
      if (m[1] === 'name' && value) name = value
      else if (m[1] === 'description' && value) description = value
    }
  }
  return { name, description, body: lines.slice(bodyStart).join('\n').trim() }
}

/** 列出专家插件的团队成员(agents/*.md，排除 leader 自身)；目录不存在返回空 */
export function listTeamMembers(pluginDir: string, leader?: string): TeamMember[] {
  const agentsDir = join(pluginDir, 'agents')
  if (!existsSync(agentsDir)) return []
  const out: TeamMember[] = []
  for (const entry of readdirSync(agentsDir, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.toLowerCase().endsWith('.md')) continue
    const base = entry.name.slice(0, -3)
    if (leader && base === leader) continue
    const parsed = parseAgentMarkdown(readFileSync(join(agentsDir, entry.name), 'utf8'), base)
    out.push({ name: base, description: parsed.description })
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
}

/** 读成员提示词正文；缺失回退通用文案 */
export function readMemberPrompt(pluginDir: string, member: string): string {
  const file = join(pluginDir, 'agents', `${member}.md`)
  if (!existsSync(file)) return `你是团队成员「${member}」。`
  return parseAgentMarkdown(readFileSync(file, 'utf8'), member).body || `你是团队成员「${member}」。`
}

/**
 * 团队编排工具 task：模型传入 member + task，交给注入的 run 执行成员子代理。
 * kind=read（不弹权限；成员内部的写操作仍各自经权限网关）。
 */
export function createTaskTool(opts: {
  members: TeamMember[]
  run: (member: string, task: string) => Promise<{ ok: boolean; output: string }>
}): ToolDefinition {
  const roster = opts.members.map((m) => `- ${m.name}${m.description ? `：${m.description}` : ''}`).join('\n')
  return {
    name: 'task',
    description: `把子任务派给团队成员执行并返回其结果。可用成员：\n${roster}`,
    kind: 'read',
    parameters: {
      type: 'object',
      properties: {
        member: { type: 'string', description: '成员名（见可用成员列表）' },
        task: { type: 'string', description: '交给该成员完成的子任务描述' }
      },
      required: ['member', 'task']
    },
    execute: async (args) => {
      const member = String(args.member ?? '').trim()
      const task = String(args.task ?? '').trim()
      if (!opts.members.some((m) => m.name === member)) {
        return { ok: false, output: `未知成员: ${member}（可用：${opts.members.map((m) => m.name).join(', ')}）` }
      }
      if (!task) return { ok: false, output: '缺少 task 子任务描述' }
      return opts.run(member, task)
    }
  }
}
