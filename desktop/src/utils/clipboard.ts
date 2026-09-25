import MarkdownIt from 'markdown-it'

/**
 * 剪贴板工具(消息级操作 C1):
 *   - copyToClipboard: 优先 Clipboard API, 失败回退 textarea + execCommand(file:// 兼容)
 *   - markdownToPlainText: 复制「纯文本」时剥离 Markdown 语法(渲染为 HTML 后剥标签)
 */

const plainMd = new MarkdownIt({ html: false, linkify: true, breaks: true })

const ENTITIES: Record<string, string> = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#39;': "'",
  '&nbsp;': ' '
}

/** Markdown → 纯文本: 段落/换行保留, 列表项单换行, 图片取 alt, 其余标签剥离 */
export function markdownToPlainText(src: string): string {
  const html = plainMd.render(src ?? '')
  const text = html
    .replace(/<\/li>\s*/gi, '\n')
    .replace(/<\/?(ul|ol)[^>]*>\s*/gi, '')
    .replace(/<img[^>]*alt="([^"]*)"[^>]*\/?>/gi, '$1')
    .replace(/<\/(p|div|h[1-6]|blockquote|pre|tr)>/gi, '\n\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (m) => ENTITIES[m] ?? m)
  return text.replace(/\n{3,}/g, '\n\n').trim()
}

/** 写剪贴板: 返回是否成功(不支持时回退旧接口, 不抛异常) */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走回退分支
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}
