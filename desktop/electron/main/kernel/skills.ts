import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

/**
 * SKILL.md 技能加载器：扫描 skills 目录下的 <name>/SKILL.md。
 * front-matter 仅支持文件开头 `---` 围栏 + 行级 `key: value`（name/description），
 * 不做 YAML 全量解析、不引依赖，与 agent/ 项目的 skill 格式同构。
 */
export interface SkillMeta {
  name: string
  description: string
}

export interface SkillLoader {
  listSkills(): SkillMeta[]
  getSkillBody(name: string): string
  systemPromptAddendum(): string
}

/** 单个技能的完整解析结果 */
interface ParsedSkill {
  name: string
  description: string
  body: string
}

/**
 * 解析单个 SKILL.md 内容：
 * - 首行必须是 `---`，且存在第二个 `---` 行闭合 front-matter，否则视为坏文件返回 null
 * - 围栏内逐行匹配 `key: value`，只认 name/description，值支持成对引号
 * - 正文取闭合围栏之后到文件尾；若文件以 `---` 结尾围栏收尾（其后无内容）则止于该行，
 *   正文中间的 `---` 分隔线原样保留不误吞
 */
function parseSkillMd(dirName: string, raw: string): ParsedSkill | null {
  const lines = raw.split(/\r?\n/)
  if (lines[0]?.trim() !== '---') return null
  let close = -1
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') {
      close = i
      break
    }
  }
  if (close === -1) return null

  let name: string | undefined
  let description: string | undefined
  for (let i = 1; i < close; i++) {
    const m = lines[i].match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/)
    if (!m) continue
    const value = m[2].trim().replace(/^["']|["']$/g, '')
    if (m[1] === 'name' && name === undefined) name = value
    else if (m[1] === 'description' && description === undefined) description = value
  }
  if (!name || !description) return null

  // 以目录名为准，防止 front-matter 重名冲突
  if (name !== dirName) {
    console.warn(`技能 ${dirName} 的 front-matter name「${name}」与目录名不一致，已以目录名为准`)
    name = dirName
  }

  // 正文：先收掉尾部空行，再容忍可选的 `---` 结尾围栏
  const bodyLines = lines.slice(close + 1)
  while (bodyLines.length && bodyLines[bodyLines.length - 1].trim() === '') bodyLines.pop()
  if (bodyLines.length && bodyLines[bodyLines.length - 1].trim() === '---') bodyLines.pop()
  return { name, description, body: bodyLines.join('\n').trim() }
}

export function createSkillLoader(skillsDir: string): SkillLoader {
  /** 扫描目录并解析全部技能；目录不存在或条目无效时静默跳过 */
  function loadAll(): ParsedSkill[] {
    if (!existsSync(skillsDir)) return []
    const skills: ParsedSkill[] = []
    for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      const file = join(skillsDir, entry.name, 'SKILL.md')
      if (!existsSync(file)) continue
      const parsed = parseSkillMd(entry.name, readFileSync(file, 'utf-8'))
      if (parsed) skills.push(parsed)
    }
    return skills.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  }

  return {
    listSkills() {
      return loadAll().map(s => ({ name: s.name, description: s.description }))
    },
    getSkillBody(name) {
      const skill = loadAll().find(s => s.name === name)
      if (!skill) throw new Error(`未找到技能: ${name}`)
      return skill.body
    },
    systemPromptAddendum() {
      const skills = loadAll()
      if (!skills.length) return ''
      const items = skills.map(s => `- ${s.name}：${s.description}`).join('\n')
      return `## 可用技能\n\n以下技能可通过「/技能名」触发：\n\n${items}`
    }
  }
}
