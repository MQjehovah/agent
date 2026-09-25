import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pickScreenSource, screenshotFailureText } from '../../electron/main/screenshot'

/** 截图(E)主进程纯逻辑: 主屏选源与失败文案 */

test('screenshot: 优先匹配主屏 display_id, 其次退化第一屏', () => {
  const sources = [
    { id: 'screen:1:0', display_id: '100' },
    { id: 'screen:2:0', display_id: '200' }
  ]
  assert.equal(pickScreenSource(sources, 200)?.id, 'screen:2:0')
  // display_id 数字/字符串混用也能命中
  assert.equal(pickScreenSource(sources, '100')?.id, 'screen:1:0')
  // 未命中(如主屏 id 与实际枚举不一致)时退化取第一屏
  assert.equal(pickScreenSource(sources, 999)?.id, 'screen:1:0')
  // 空列表返回 undefined, 由调用方给可读错误
  assert.equal(pickScreenSource([], 1), undefined)
})

test('screenshot: display_id 缺失时退化第一屏, 不抛错', () => {
  assert.equal(pickScreenSource([{ id: 'screen:only' }], 9)?.id, 'screen:only')
  const sources = [{ id: 'screen:1', display_id: '' }, { id: 'screen:2', display_id: '9' }]
  assert.equal(pickScreenSource(sources, 9)?.id, 'screen:2')
})

test('screenshot: 失败文案区分权限/一般失败/空原因', () => {
  assert.equal(screenshotFailureText(new Error('Permission denied')), '截图失败：无屏幕录制权限（Permission denied）')
  assert.match(screenshotFailureText('屏幕捕获被拒绝'), /无屏幕录制权限/)
  assert.equal(screenshotFailureText(new Error('没有可用的屏幕源')), '截图失败：没有可用的屏幕源')
  assert.equal(screenshotFailureText(undefined), '截图失败：未获取到屏幕画面')
  assert.equal(screenshotFailureText('   '), '截图失败：未获取到屏幕画面')
})
