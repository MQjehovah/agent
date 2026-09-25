/**
 * OIDC 客户端配置解析(纯函数,不依赖 Electron,便于单测)。
 * client_id 非敏感,保留默认值;issuer 不再内置兜底,缺失即抛错强制注入;
 * client_secret 可选(公共客户端 + PKCE 无需密钥,机密部署可注入)。
 */

/** 解析 OIDC issuer:env 优先,其次本地配置,两者皆空则抛出可操作错误;去掉末尾斜杠。 */
export function resolveOidcIssuer(envIssuer?: string, configIssuer?: string): string {
  const issuer = (envIssuer ?? '').trim() || (configIssuer ?? '').trim()
  if (!issuer) {
    throw new Error('未配置 OIDC Issuer(请由安装包或企业配置注入),无法完成企业 SSO 登录')
  }
  return issuer.replace(/\/+$/, '')
}

/** 解析 OIDC client_secret:env 优先,其次本地配置;未配置返回空串(公共客户端 + PKCE,不随身携带密钥)。 */
export function resolveOidcClientSecret(envSecret?: string, configSecret?: string): string {
  return (envSecret ?? '').trim() || (configSecret ?? '').trim()
}

/** 解析 OIDC client_id:env 优先,其次本地配置,最后回退非敏感默认值。 */
export function resolveOidcClientId(envId?: string, configId?: string): string {
  return (envId ?? '').trim() || (configId ?? '').trim() || 'dashboard-gateway'
}
