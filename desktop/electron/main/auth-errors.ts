/**
 * 网关管理端鉴权失败的可读文案与错误体解析(usage 与 router 凭据共用)。
 */

/** 401/403 的可读错误;其它状态码返回 null,由调用方按各自语义兜底 */
export function readableAuthError(status: number, detail?: string): Error | null {
  if (status === 401) {
    return Object.assign(new Error('企业认证已过期,请重新登录企业账号'), { status })
  }
  if (status === 403) {
    const suffix = detail && detail.trim() ? `:${detail.trim()}` : ''
    return Object.assign(new Error(`企业账号未开通算力网关,请联系管理员${suffix}`), { status })
  }
  return null
}

/** 尽力从错误响应体读取 detail;解析失败或无 detail 返回 undefined */
export async function readErrorDetail(res: Response): Promise<string | undefined> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    return typeof body?.detail === 'string' ? body.detail : undefined
  } catch {
    return undefined
  }
}
