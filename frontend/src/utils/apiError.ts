import axios from 'axios'
import type { ApiResponse } from '@/types'

export function getApiErrorMessage(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const responseData = error.response?.data as (ApiResponse & { detail?: unknown; msg?: unknown }) | string | undefined

    if (typeof responseData === 'string' && responseData.trim()) {
      return responseData
    }

    if (responseData && typeof responseData === 'object') {
      const detail = responseData.detail
      const nestedMessage = detail && typeof detail === 'object' && 'message' in detail ? (detail as { message?: unknown }).message : undefined
      const message = responseData.message || responseData.msg || nestedMessage || (typeof detail === 'string' ? detail : undefined)
      if (typeof message === 'string' && message.trim()) {
        return message
      }

      // FastAPI/Pydantic validation errors are returned as a detail array.
      // Convert the first field error into a readable message instead of
      // falling through to a generic network-style message.
      if (Array.isArray(detail)) {
        const firstError = detail[0] as { msg?: unknown; loc?: unknown } | undefined
        if (typeof firstError?.msg === 'string' && firstError.msg.trim()) {
          const field = Array.isArray(firstError.loc) ? firstError.loc.at(-1) : undefined
          const fieldNames: Record<string, string> = {
            username: '用户名',
            invite_code: '邀请码',
            password: '密码',
          }
          const fieldLabel = typeof field === 'string' ? fieldNames[field] : undefined
          return fieldLabel ? `${fieldLabel}：${firstError.msg}` : firstError.msg
        }
      }
    }

    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT' || error.message?.toLowerCase().includes('timeout')) {
      return '请求超时，请稍后重试'
    }

    if (!error.response && error.message?.toLowerCase() === 'network error') {
      return '无法连接服务器，请检查网络或确认服务已启动'
    }

    if (error.message?.trim()) {
      return error.message
    }
  }

  if (error instanceof Error && error.message.trim()) {
    return error.message
  }

  return fallback
}
