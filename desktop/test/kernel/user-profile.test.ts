import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildUserProfileLine } from '../../electron/main/kernel/user-profile'

const GUIDE = '回答"我/我的"相关问题时以该用户为准；不要向该用户询问他自己已提供的信息。'

test('buildUserProfileLine：工号+姓名+部门组合（与参考文案一致）', () => {
  const line = buildUserProfileLine({ id: '202202100024', name: '张明', department: '数字中台部' })
  assert.equal(
    line,
    `当前用户：张明（工号 202202100024）；部门：数字中台部。${GUIDE}`
  )
})

test('buildUserProfileLine：仅有姓名（无工号/部门）', () => {
  const line = buildUserProfileLine({ name: '张明' })
  assert.equal(line, `当前用户：张明。${GUIDE}`)
})

test('buildUserProfileLine：仅有工号时回退为姓名且不重复工号', () => {
  const line = buildUserProfileLine({ id: '202202100024' })
  assert.equal(line, `当前用户：202202100024。${GUIDE}`)
})

test('buildUserProfileLine：姓名/工号皆无返回空串（不注入）', () => {
  assert.equal(buildUserProfileLine(), '')
  assert.equal(buildUserProfileLine({}), '')
  assert.equal(buildUserProfileLine({ name: '   ', id: '\n', department: '数字中台部' }), '')
})

test('buildUserProfileLine：换行/连续空白展平为单行', () => {
  const line = buildUserProfileLine({
    id: '2022\n0210 0024',
    name: ' 张\n明 ',
    department: '数字  中台\n部'
  })
  assert.ok(!line.includes('\n'), line)
  assert.equal(
    line,
    `当前用户：张 明（工号 2022 0210 0024）；部门：数字 中台 部。${GUIDE}`
  )
})

test('buildUserProfileLine：空部门不追加「部门」段', () => {
  const line = buildUserProfileLine({ id: '1001', name: '李雷', department: '  ' })
  assert.equal(line, `当前用户：李雷（工号 1001）。${GUIDE}`)
})

test('buildUserProfileLine：含钉钉 userId，顺序为工号→部门→钉钉', () => {
  const line = buildUserProfileLine({
    id: '202202100024',
    name: '张明',
    department: '数字中台部',
    dingtalk: 'dt_123456'
  })
  assert.equal(
    line,
    `当前用户：张明（工号 202202100024）；部门：数字中台部；钉钉 userId：dt_123456。${GUIDE}`
  )
  assert.ok(line.indexOf('工号') < line.indexOf('部门'), line)
  assert.ok(line.indexOf('部门') < line.indexOf('钉钉 userId'), line)
})

test('buildUserProfileLine：无钉钉号不出现该段；钉钉号换行展平', () => {
  const without = buildUserProfileLine({ id: '1001', name: '李雷', department: '信息部' })
  assert.ok(!without.includes('钉钉'), without)

  const line = buildUserProfileLine({ id: '1001', name: '李雷', dingtalk: 'dt\n123  456' })
  assert.ok(!line.includes('\n'), line)
  assert.ok(line.includes('；钉钉 userId：dt 123 456。'), line)
})
