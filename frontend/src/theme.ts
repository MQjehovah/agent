import { ref } from 'vue'

const KEY = 'agent_theme'
export type Theme = 'dark' | 'light'

const theme = ref<Theme>((localStorage.getItem(KEY) as Theme) ?? 'dark')

function apply() {
  document.documentElement.classList.toggle('dark', theme.value === 'dark')
  document.documentElement.classList.toggle('light', theme.value === 'light')
}

export function useTheme() {
  const toggle = () => {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
    localStorage.setItem(KEY, theme.value)
    apply()
  }
  apply()
  return { theme, toggle }
}
