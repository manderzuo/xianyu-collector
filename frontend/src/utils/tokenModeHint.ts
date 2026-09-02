const TOKEN_MODE_HINT_KEY = 'xianyu-token-mode-hint-dismissed-v1'

export function isTokenModeHintDismissed(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(TOKEN_MODE_HINT_KEY) === '1'
  } catch {
    return false
  }
}

export function dismissTokenModeHint(): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(TOKEN_MODE_HINT_KEY, '1')
  } catch {
    // 浏览器禁用本地存储时仍允许本次页面关闭提示。
  }
}
