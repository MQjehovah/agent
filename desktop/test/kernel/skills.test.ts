import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createSkillLoader } from '../../electron/main/kernel/skills'

/** 建一个临时 skills 目录，用完由测试末尾统一清理 */
function makeSkillsDir(): string {
  return mkdtempSync(join(tmpdir(), 'skills-'))
}

/** 写一个标准技能目录：<dir>/<name>/SKILL.md */
function writeSkill(dir: string, name: string, fm: string, body: string): void {
  const skillDir = join(dir, name)
  mkdirSync(skillDir, { recursive: true })
  writeFileSync(join(skillDir, 'SKILL.md'), `---\n${fm}\n---\n${body}`)
}

test('skills: listSkills 扫描两个技能目录', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'commit-helper', 'name: commit-helper\ndescription: 按约定式提交规范生成 commit message', '正文A')
    writeSkill(dir, 'release-notes', 'name: release-notes\ndescription: 生成发布说明', '正文B')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [
      { name: 'commit-helper', description: '按约定式提交规范生成 commit message' },
      { name: 'release-notes', description: '生成发布说明' }
    ])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: 坏 front-matter 的目录跳过不抛（缺字段 / 无围栏）', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'good', 'name: good\ndescription: 好技能', '正文')
    // 缺 description
    writeSkill(dir, 'no-desc', 'name: no-desc', '正文')
    // 缺 name
    writeSkill(dir, 'no-name', 'description: 只有描述', '正文')
    // 无 --- 围栏
    const noFence = join(dir, 'no-fence')
    mkdirSync(noFence)
    writeFileSync(join(noFence, 'SKILL.md'), 'name: no-fence\ndescription: 没有围栏')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'good', description: '好技能' }])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: getSkillBody 返回围栏之后的正文（trim 后非空）', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'demo', 'name: demo\ndescription: 演示', '\n第一步：做事。\n第二步：收尾。\n')
    const loader = createSkillLoader(dir)
    assert.equal(loader.getSkillBody('demo'), '第一步：做事。\n第二步：收尾。')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: getSkillBody 未知名称抛中文错误', () => {
  const dir = makeSkillsDir()
  try {
    const loader = createSkillLoader(dir)
    assert.throws(() => loader.getSkillBody('ghost'), /未找到技能/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: systemPromptAddendum 无技能返回空串', () => {
  const dir = makeSkillsDir()
  try {
    const loader = createSkillLoader(dir)
    assert.equal(loader.systemPromptAddendum(), '')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: systemPromptAddendum 含中文标题与每个技能的 name+description', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'commit-helper', 'name: commit-helper\ndescription: 按约定式提交规范生成 commit message', '正文A')
    writeSkill(dir, 'release-notes', 'name: release-notes\ndescription: 生成发布说明', '正文B')
    const addendum = createSkillLoader(dir).systemPromptAddendum()
    assert.ok(addendum.includes('## 可用技能'))
    assert.ok(addendum.includes('commit-helper'))
    assert.ok(addendum.includes('按约定式提交规范生成 commit message'))
    assert.ok(addendum.includes('release-notes'))
    assert.ok(addendum.includes('生成发布说明'))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: skillsDir 不存在时返回空列表与空 addendum，不抛', () => {
  const loader = createSkillLoader(join(tmpdir(), 'skills-not-exist-' + Date.now()))
  assert.deepEqual(loader.listSkills(), [])
  assert.equal(loader.systemPromptAddendum(), '')
  assert.throws(() => loader.getSkillBody('any'))
})

test('skills: 非技能文件与无 SKILL.md 的子目录忽略', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'good', 'name: good\ndescription: 好技能', '正文')
    writeFileSync(join(dir, 'README.md'), '散落文件，不是技能')
    mkdirSync(join(dir, 'empty-dir'))
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'good', description: '好技能' }])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: front-matter name 与目录名不一致时以目录名为准', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'real-name', 'name: fake-name\ndescription: 描述', '正文')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'real-name', description: '描述' }])
    assert.equal(loader.getSkillBody('real-name'), '正文')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: CRLF 文件正常解析', () => {
  const dir = makeSkillsDir()
  try {
    const skillDir = join(dir, 'crlf')
    mkdirSync(skillDir)
    writeFileSync(join(skillDir, 'SKILL.md'), '---\r\nname: crlf\r\ndescription: 视窗换行\r\n---\r\n正文一\r\n正文二\r\n')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'crlf', description: '视窗换行' }])
    assert.equal(loader.getSkillBody('crlf'), '正文一\n正文二')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: 正文中的 --- 分隔线不误吞', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'sep', 'name: sep\ndescription: 分隔线', '前半部分\n---\n后半部分')
    const loader = createSkillLoader(dir)
    assert.equal(loader.getSkillBody('sep'), '前半部分\n---\n后半部分')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: 正文可选 --- 结尾围栏被剥离，两种形式都容忍', () => {
  const dir = makeSkillsDir()
  try {
    // 形式一：正文以 --- 围栏收尾
    const fenced = join(dir, 'fenced')
    mkdirSync(fenced)
    writeFileSync(join(fenced, 'SKILL.md'), '---\nname: fenced\ndescription: 带尾围栏\n---\n正文内容\n---\n')
    // 形式二：正文直接到文件尾
    writeSkill(dir, 'plain', 'name: plain\ndescription: 无尾围栏', '正文内容')
    const loader = createSkillLoader(dir)
    assert.equal(loader.getSkillBody('fenced'), '正文内容')
    assert.equal(loader.getSkillBody('plain'), '正文内容')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: description 含冒号的行正常解析', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'colon', 'name: colon\ndescription: 时间格式：HH:MM:SS，注意冒号', '正文')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'colon', description: '时间格式：HH:MM:SS，注意冒号' }])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('skills: description 带成对引号的值去掉引号', () => {
  const dir = makeSkillsDir()
  try {
    writeSkill(dir, 'quoted', 'name: quoted\ndescription: "带引号的描述"', '正文')
    const loader = createSkillLoader(dir)
    assert.deepEqual(loader.listSkills(), [{ name: 'quoted', description: '带引号的描述' }])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
