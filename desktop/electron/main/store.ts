import { app } from 'electron'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { buildDefaultConfig, createConfigStore, type AppConfig } from './config-core'

export type { AppConfig }

function configPath(): string {
  return join(app.getPath('userData'), 'config.json')
}

const configStore = createConfigStore(
  buildDefaultConfig({ home: app.getPath('home'), env: process.env }),
  {
    readText: () => (existsSync(configPath()) ? readFileSync(configPath(), 'utf-8') : null),
    writeText: (text) => {
      mkdirSync(app.getPath('userData'), { recursive: true })
      writeFileSync(configPath(), text, 'utf-8')
    }
  }
)

/** 读取 config.json 原始内容(不合并默认值);文件不存在/解析失败返回 {}。seed 注入判定必须基于此。 */
export function getStoredConfig(): Partial<AppConfig> {
  return configStore.stored()
}

export function getConfig(): AppConfig {
  return configStore.get()
}

export function updateConfig(patch: Partial<AppConfig>): AppConfig {
  return configStore.update(patch)
}
