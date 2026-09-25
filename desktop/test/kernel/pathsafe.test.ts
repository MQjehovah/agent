import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveWithin, isWithin, PathEscapeError } from '../../electron/main/kernel/pathsafe'

test('pathsafe: relative path resolves inside workspace', () => {
  assert.ok(isWithin('C:\\ws', resolveWithin('C:\\ws', 'a\\b.txt')))
})

test('pathsafe: dotdot escape rejected', () => {
  assert.throws(() => resolveWithin('C:\\ws', '..\\escape.txt'))
})

test('pathsafe: absolute path inside allowed, sibling rejected', () => {
  assert.ok(isWithin('C:\\ws', resolveWithin('C:\\ws', 'C:\\ws\\x.txt')))
  assert.throws(() => resolveWithin('C:\\ws', 'C:\\ws2\\x.txt'))
})

test('pathsafe: workspace path is case-insensitive on win32', () => {
  // 项目目标平台为 Windows，win32 下归一化做大小写折叠
  assert.ok(isWithin('C:\\ws', resolveWithin('C:\\WS', 'X.TXT')))
})

test('pathsafe: workspace itself is within', () => {
  assert.ok(isWithin('C:\\ws', 'C:\\ws'))
})

test('pathsafe: sibling directory rejected with PathEscapeError', () => {
  assert.throws(() => resolveWithin('C:\\ws', 'C:\\ws2\\x.txt'), PathEscapeError)
})

test('pathsafe: deep dotdot escape throws PathEscapeError', () => {
  assert.throws(() => resolveWithin('C:\\ws', 'a\\..\\..'), PathEscapeError)
})

test('pathsafe: relative workspace rejected as non-absolute', () => {
  assert.throws(() => resolveWithin('ws', 'x.txt'), /必须是绝对路径/)
  assert.throws(() => isWithin('ws', 'ws\\x.txt'), /必须是绝对路径/)
})
