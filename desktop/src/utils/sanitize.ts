/**
 * LLM 输出 HTML 安全清洗(界面直接渲染"邮件级" HTML 报告):
 * - markdown-it html:true 放开的原始 HTML 先经 DOMPurify 白名单清洗, 再 v-html;
 * - 仅保留表格/排版级标签与受限内联样式(条形图/指标卡所需), 禁脚本/事件/iframe/表单;
 * - style 值过滤: 属性白名单 + 拒绝 url()/expression/javascript:;
 * - URI 白名单含 xzmedia(主进程媒体代理)与 blob。
 */
import DOMPurify from 'dompurify'

/** 允许的内联样式属性(邮件级报告的条形图/指标卡够用) */
const STYLE_ALLOW =
  /^(background|background-color|color|font|font-size|font-weight|font-family|line-height|letter-spacing|text-align|text-indent|vertical-align|white-space|width|min-width|max-width|height|min-height|margin|margin-(top|right|bottom|left)|padding|padding-(top|right|bottom|left)|border|border-(top|right|bottom|left|collapse|radius|color|width|style)|border-radius|display|table-layout)$/i
/** 值里出现即丢弃 */
const VALUE_DENY = /url\s*\(|expression\s*\(|javascript:/i

function filterStyle(style: string): string {
  const out: string[] = []
  for (const part of String(style || '').split(';')) {
    const idx = part.indexOf(':')
    if (idx <= 0) continue
    const prop = part.slice(0, idx).trim()
    const value = part.slice(idx + 1).trim()
    if (!prop || !value) continue
    if (!STYLE_ALLOW.test(prop)) continue
    if (VALUE_DENY.test(value)) continue
    if (prop.toLowerCase() === 'display' && !/^(block|inline|inline-block|table|table-row|table-cell|none)$/i.test(value)) continue
    out.push(`${prop}:${value}`)
  }
  return out.join(';')
}

let hooked = false
function ensureHook(): void {
  if (hooked) return
  hooked = true
  DOMPurify.addHook('uponSanitizeAttribute', (_node, data) => {
    if (data.attrName === 'style') data.attrValue = filterStyle(data.attrValue)
  })
}

/** 清洗后的 HTML(可直接 v-html; mermaid 占位块 data-src 保留) */
export function sanitizeHtml(html: string): string {
  ensureHook()
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['data-src', 'target'],
    // 自定义 scheme: xzmedia(主进程媒体代理)/blob; 不含 data:
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel|callto|sms|cid|xmpp|xzmedia|blob):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
    FORBID_TAGS: [
      'style', 'iframe', 'frame', 'frameset', 'form', 'input', 'button', 'select',
      'textarea', 'link', 'meta', 'base', 'video', 'audio', 'object', 'embed', 'svg', 'math'
    ]
  })
}
