/**
 * Mermaid 图表渲染:懒加载 mermaid(单独分包), 扫描容器内的 .mermaid-block
 * 占位节点, 画成 SVG 后回填。失败时保留源码, 不阻断消息渲染。
 */
let mermaidReady: Promise<typeof import('mermaid').default> | null = null

function loadMermaid(): Promise<typeof import('mermaid').default> {
  if (!mermaidReady) {
    mermaidReady = import('mermaid').then((mod) => {
      const mermaid = mod.default
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: document.documentElement.classList.contains('dark') ? 'dark' : 'default',
        fontFamily: 'inherit'
      })
      return mermaid
    })
  }
  return mermaidReady
}

function decodeBase64Utf8(b64: string): string {
  try {
    const binary = atob(b64)
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return ''
  }
}

let seq = 0

/** 渲染容器内所有尚未处理的 mermaid 占位块; 可重复调用(内容更新后) */
export async function renderMermaidIn(root: HTMLElement): Promise<void> {
  const nodes = Array.from(root.querySelectorAll<HTMLElement>('.mermaid-block[data-src]'))
  if (nodes.length === 0) return
  let mermaid: typeof import('mermaid').default
  try {
    mermaid = await loadMermaid()
  } catch {
    return
  }
  for (const node of nodes) {
    if (node.dataset.done === '1') continue
    const code = decodeBase64Utf8(node.dataset.src ?? '')
    if (!code.trim()) continue
    node.dataset.done = '1'
    const id = `mmd-${Date.now().toString(36)}-${seq++}`
    try {
      const { svg } = await mermaid.render(id, code)
      node.innerHTML = svg
      node.classList.add('mermaid-done')
    } catch (err) {
      node.dataset.done = '0'
      node.classList.add('mermaid-failed')
      node.textContent = `Mermaid 渲染失败:${(err as Error).message}\n\n${code}`
    }
  }
}
