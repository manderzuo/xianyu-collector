import { API_PREFIX } from '../config'
import { getStructuredErrorMessage, notifyVipContentIfNeeded } from '@/utils/vipContent'

export type ApiResult<T = unknown> = { success: boolean; code: string; message: string; data: T }

export async function request<T = unknown>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  const token = localStorage.getItem('xr_token')
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type') && init?.body) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(path.startsWith('/api') ? path : `${API_PREFIX}${path}`, { ...init, headers })
  const body = await response.json().catch(() => ({ success: false, message: '服务返回异常', data: null }))
  if (!response.ok) {
    notifyVipContentIfNeeded(response.status, body)
    throw new Error(getStructuredErrorMessage(body, `请求失败（${response.status}）`))
  }
  return body as ApiResult<T>
}

export const get = <T = unknown>(path: string) => request<T>(path)
export const post = <T = unknown>(path: string, data?: unknown) => request<T>(path, { method: 'POST', body: data === undefined ? undefined : JSON.stringify(data) })
