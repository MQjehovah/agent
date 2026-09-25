import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'

/**
 * LLM 输出渲染:html 关闭,原始 HTML 一律转义;代码走 highlight.js 高亮;
 * ```mermaid 代码块渲染为占位节点,由 utils/mermaid.ts 在挂载后异步画成图。
 * 链接统一新窗口打开(main 进程会转交系统浏览器)。
 */
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(code: string, lang: string): string {
    const language = (lang || '').trim().toLowerCase()
    if (language && hljs.getLanguage(language)) {
      try {
        const html = hljs.highlight(code, { language, ignoreIllegals: true }).value
        return `<pre class="hljs"><code class="language-${language}">${html}</code></pre>`
      } catch {
        // 落到转义分支
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`
  }
})

const defaultLinkOpen = md.renderer.rules.link_open

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, idx, options, env, self)
    : self.renderToken(tokens, idx, options)
}

/** 主进程媒体代理 scheme(见 electron/main/media.ts);CSP 已放行 xzmedia: */
const MEDIA_SCHEME = 'xzmedia'

const defaultImage = md.renderer.rules.image

// 外部/相对图片统一走 xzmedia://fetch?u=..., 由主进程带鉴权取回(绕过 CSP 与 OSS 防盗链)
md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const src = String(token.attrGet('src') ?? '')
  if (src && !/^(xzmedia|data|blob):/i.test(src)) {
    token.attrSet('src', `${MEDIA_SCHEME}://fetch?u=${encodeURIComponent(src)}`)
  }
  return defaultImage
    ? defaultImage(tokens, idx, options, env, self)
    : self.renderToken(tokens, idx, options)
}

/** mermaid 源码用 base64 放进 data-src, 避免 HTML 转义与提前执行 */
function base64Utf8(text: string): string {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary)
}

const defaultFence = md.renderer.rules.fence

md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const info = tokens[idx].info.trim().toLowerCase()
  if (info === 'mermaid' || info === 'graph' || info === 'sequence') {
    const code = tokens[idx].content
    return `<div class="mermaid-block" data-src="${base64Utf8(code)}">Mermaid 图表渲染中…</div>`
  }
  return defaultFence
    ? defaultFence(tokens, idx, options, env, self)
    : self.renderToken(tokens, idx, options)
}

/** 给每个代码块加"复制"按钮(点击由 MarkdownBody 的事件代理处理) */
const defaultPre = md.renderer.rules.code_block ?? null

export function renderMarkdown(text: string): string {
  const html = md.render(text ?? '')
  void defaultPre
  // 包一层容器便于右上角放复制按钮(纯字符串处理, 不引入额外解析)
  return html.replace(
    /<pre class="hljs">/g,
    '<div class="code-wrap"><button class="code-copy" type="button">复制</button><pre class="hljs">'
  ).replace(/<\/pre>/g, '</pre></div>')
}
