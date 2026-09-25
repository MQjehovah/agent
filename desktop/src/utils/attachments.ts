/**
 * 渲染层附件提示文案（与主进程 `electron/main/kernel/attachments.ts` 刻意同构）。
 *
 * 主进程模块无法被渲染层 import，故此处复制同一实现；两处一致性由
 * `test/kernel/attachments.test.ts` 的等值测试锁定（同时 import 两份实现并断言输出相同），
 * 任何改动必须同步，否则消息文本会出现两种格式。
 */
export function formatAttachmentNotice(relPaths: string[]): string {
  if (relPaths.length === 0) return ''
  return `\n\n已附带文件：\n${relPaths.map((p) => `- ${p}`).join('\n')}`
}

/** 是否可发送：有文本或有待发附件，且未在流式中；抽为纯函数以便单测 */
export function canSendMessage(text: string, attachmentCount: number, streaming: boolean): boolean {
  return (text.trim().length > 0 || attachmentCount > 0) && !streaming
}
