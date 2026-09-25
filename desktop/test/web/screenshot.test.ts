import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  dataUrlToBytes,
  screenshotFailureMessage,
  screenshotFileName
} from '../../src/utils/screenshot'

/** 截图(E)渲染层纯逻辑: dataURL 解码、附件命名与失败兜底文案 */

test('screenshot: dataURL 解码为原始字节', () => {
  // base64("ABC") = "QUJD"
  const bytes = dataUrlToBytes('data:image/png;base64,QUJD')
  assert.deepEqual(Array.from(bytes), [65, 66, 67])
})

test('screenshot: 空/损坏 dataURL 抛可读错误', () => {
  assert.throws(() => dataUrlToBytes(''), /截图数据无效/)
  assert.throws(() => dataUrlToBytes('data:image/png,QUJD'), /仅支持 base64/)
  assert.throws(() => dataUrlToBytes('data:image/png;base64,!!!'), /解码失败/)
})

test('screenshot: 附件名带秒级时间戳且固定 png 后缀', () => {
  const name = screenshotFileName(new Date(2026, 8, 22, 9, 5, 3).getTime())
  assert.equal(name, 'screenshot-20260922-090503.png')
})

test('screenshot: 失败文案兜底不丢原因', () => {
  assert.equal(screenshotFailureMessage(new Error('没有可用的屏幕源')), '截图失败：没有可用的屏幕源')
  assert.equal(screenshotFailureMessage('捕获被拒绝'), '截图失败：捕获被拒绝')
  assert.equal(screenshotFailureMessage(undefined), '截图失败：未获取到屏幕画面')
  assert.equal(screenshotFailureMessage(new Error('  ')), '截图失败：未获取到屏幕画面')
})
