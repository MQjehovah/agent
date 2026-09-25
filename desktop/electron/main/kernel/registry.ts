import type { OpenAiTool, ToolDefinition } from './types'

/**
 * 工具注册表：按名称管理内置工具与 MCP 工具，
 * 并能导出 OpenAI function calling 形式的 tools 描述。
 */
export interface Registry {
  register(tool: ToolDefinition): void
  /** 按名摘除工具；未注册过返回 false。供重装覆盖 / 卸载市场工具 / MCP 失效重建使用 */
  unregister(name: string): boolean
  get(name: string): ToolDefinition | undefined
  list(): ToolDefinition[]
  toOpenAiTools(): OpenAiTool[]
}

export function createRegistry(): Registry {
  const tools = new Map<string, ToolDefinition>()
  return {
    register(tool) {
      if (tools.has(tool.name)) throw new Error(`tool already registered: ${tool.name}`)
      tools.set(tool.name, tool)
    },
    unregister(name) {
      return tools.delete(name)
    },
    get: (name) => tools.get(name),
    list: () => [...tools.values()],
    toOpenAiTools: () => [...tools.values()].map(t => ({
      type: 'function',
      function: { name: t.name, description: t.description, parameters: t.parameters }
    }))
  }
}
