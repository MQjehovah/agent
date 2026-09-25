import { test } from 'node:test'
import assert from 'node:assert/strict'
import { markdownToPlainText } from '../../src/utils/clipboard'

/** 复制整条消息(C1)的纯文本转换: Markdown 语法剥离, 代码块内容保留 */

test('clipboard: markdownToPlainText 剥离常见 Markdown 语法', () => {
  assert.equal(markdownToPlainText('**加粗** 与 `代码`'), '加粗 与 代码')
  assert.equal(markdownToPlainText('# 标题\n\n正文'), '标题\n\n正文')
  assert.equal(markdownToPlainText('- 第一项\n- 第二项'), '第一项\n第二项')
  assert.equal(markdownToPlainText('[链接](https://example.com)'), '链接')
  assert.equal(markdownToPlainText('![图](https://example.com/a.png)'), '图')
})

test('clipboard: markdownToPlainText 保留代码块内容与转义字符', () => {
  assert.equal(markdownToPlainText('```ts\nconst a = 1 < 2 && a > 0\n```'), 'const a = 1 < 2 && a > 0')
  assert.equal(markdownToPlainText('a < b & c > d'), 'a < b & c > d')
  assert.equal(markdownToPlainText(''), '')
})
