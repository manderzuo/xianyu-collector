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
