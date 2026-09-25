/**
 * 附件纯逻辑：扩展名白名单、目标名清洗与消息提示文案。
 *
 * 本模块刻意不依赖 Electron / node:fs，便于单测锁定安全边界。
 * 渲染层存在同构实现 `src/utils/attachments.ts`（主进程模块无法被渲染层 import），
 * 两处格式由本文件与 `test/kernel/attachments.test.ts` 共同锁定，改动需同步。
 */

/** 允许作为附件的扩展名（小写，含点） */
export const ALLOWED_EXTS: ReadonlySet<string> = new Set([
  '.txt',
  '.md',
  '.json',
  '.csv',
  '.py',
  '.ts',
  '.js',
  '.go',
  '.java',
  '.sh',
  '.yaml',
  '.yml',
  '.log',
  '.pdf',
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.docx',
  '.xlsx',
  '.pptx'
])

/** 单文件大小上限：20MB */
export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

/**
 * 清洗文件名：只保留最后一段并移除危险片段。
 * 先统一分隔符取 basename（杜绝 `../`、绝对路径与盘符），再删去残余 `..`、
 * 控制字符与 Windows 非法字符（`:` 可在 NTFS 上构造数据流，一并清除）。
 *
 * 字符类替换后再删一次 `..`：控制字符被移除后可能重新拼出 `..`
 * （如 `.\u0000.b.txt` → `..b.txt`），若不二次剥离会破坏「清洗结果不含 `..`」的不变量。
 */
export function sanitizeAttachmentName(original: string): string {
  const base = original.replace(/\\/g, '/').split('/').pop() ?? ''
  return base
    .split('..')
    .join('')
    .replace(/[\u0000-\u001f\u007f<>:"|?*]/g, '')
    .split('..')
    .join('')
    .trim()
}

/** Windows 保留设备名（大小写不敏感，含带扩展名的形式），任何创建操作都必须拒绝 */
const WINDOWS_RESERVED_DEVICE = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i

/** 是否允许作为附件：必须无路径分隔符 / `..`、非 Windows 保留设备名，且扩展名在白名单内 */
export function isAllowedAttachment(name: string): boolean {
  if (!name || name.includes('/') || name.includes('\\') || name.includes('..')) return false
  if (WINDOWS_RESERVED_DEVICE.test(name)) return false
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return false
  return ALLOWED_EXTS.has(name.slice(dot).toLowerCase())
}

/** 目标文件名：`<ts>-<safeName>`，safeName 已去除分隔符与 `..`，保留扩展名 */
export function attachmentTargetName(original: string, ts: number): string {
  return `${ts}-${sanitizeAttachmentName(original)}`
}

/** 追加到消息文本末尾的附件提示；空列表返回空串 */
export function formatAttachmentNotice(relPaths: string[]): string {
  if (relPaths.length === 0) return ''
  return `\n\n已附带文件：\n${relPaths.map((p) => `- ${p}`).join('\n')}`
}
