import { useUIStore } from '@/store/uiStore'

type ErrorPayload = {
  code?: unknown
  detail?: unknown
  message?: unknown
}

function asRecord(value: unknown): ErrorPayload | null {
  return value && typeof value === 'object' ? value as ErrorPayload : null
}

/** 识别后端返回的套餐未开通/配额耗尽错误，并统一显示 VIP 弹窗。 */
export function notifyVipContentIfNeeded(status: number | undefined, payload: unknown): boolean {
  if (status !== 403 && status !== 409) return false
  const root = asRecord(payload)
  const detail = asRecord(root?.detail) || root
  const code = detail?.code || root?.code
  if (code !== 'feature_not_allowed' && code !== 'quota_exceeded') return false
  useUIStore.getState().showVipContentModal()
  return true
}

export function getStructuredErrorMessage(payload: unknown, fallback: string): string {
  const root = asRecord(payload)
  const detail = asRecord(root?.detail)
  const message = detail?.message || root?.message || root?.detail
  return typeof message === 'string' && message.trim() ? message : fallback
}
