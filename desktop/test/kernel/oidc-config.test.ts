import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveOidcClientId, resolveOidcClientSecret, resolveOidcIssuer } from '../../electron/main/oidc-config'

test('oidc-config：env 提供 client_secret 时直接返回', () => {
  assert.equal(resolveOidcClientSecret('env-secret', undefined), 'env-secret')
})

test('oidc-config：env 缺失时回退本地配置', () => {
  assert.equal(resolveOidcClientSecret(undefined, 'cfg-secret'), 'cfg-secret')
})

test('oidc-config：env 优先于本地配置', () => {
  assert.equal(resolveOidcClientSecret('env-secret', 'cfg-secret'), 'env-secret')
})

test('oidc-config：env 与配置均缺失时返回空串(公共客户端 + PKCE,不报错)', () => {
  assert.equal(resolveOidcClientSecret(undefined, undefined), '')
  assert.equal(resolveOidcClientSecret('', '  '), '')
})

test('oidc-config：client_id env 优先、其次配置、最后回退默认值', () => {
  assert.equal(resolveOidcClientId('env-id', 'cfg-id'), 'env-id')
  assert.equal(resolveOidcClientId(undefined, 'cfg-id'), 'cfg-id')
  assert.equal(resolveOidcClientId(undefined, undefined), 'dashboard-gateway')
})

test('oidc-config：issuer env 优先于本地配置', () => {
  assert.equal(resolveOidcIssuer('http://env:8091', 'http://cfg:8091'), 'http://env:8091')
})

test('oidc-config：issuer env 缺失时回退本地配置', () => {
  assert.equal(resolveOidcIssuer(undefined, 'http://cfg:8091'), 'http://cfg:8091')
})

test('oidc-config：issuer 仅空白视为缺失', () => {
  assert.equal(resolveOidcIssuer('  ', 'http://cfg:8091'), 'http://cfg:8091')
})

test('oidc-config：issuer env 与配置均缺失时抛出可操作的中文错误', () => {
  assert.throws(() => resolveOidcIssuer(undefined, undefined), /OIDC Issuer/)
  assert.throws(() => resolveOidcIssuer('', '  '), /无法完成企业 SSO 登录/)
})

test('oidc-config：issuer 去掉末尾斜杠', () => {
  assert.equal(resolveOidcIssuer('http://env:8091///'), 'http://env:8091')
  assert.equal(resolveOidcIssuer(undefined, 'http://cfg:8091/'), 'http://cfg:8091')
})
