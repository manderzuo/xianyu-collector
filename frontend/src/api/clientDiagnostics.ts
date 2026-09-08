import { del, get, post } from '@/utils/request'
import type { ApiResponse } from '@/types'

export type DiagnosticStatus = 'pending' | 'processing' | 'resolved' | 'ignored'
export type DiagnosticSeverity = 'info' | 'warning' | 'error' | 'critical'

export interface ClientDiagnosticReport {
  id: number
  report_code: string
  owner_user_id: number | null
  owner_username: string | null
  device_hash: string
  version: string
  build: string
  stage: string
  severity: DiagnosticSeverity
  summary: string
  metadata: Record<string, string>
  size_bytes: number
  sha256: string
  status: DiagnosticStatus
  created_at: string
  updated_at: string
  resolved_at: string | null
  expires_at: string | null
}

export interface DiagnosticStats {
  total: number
  pending: number
  processing: number
  resolved: number
  ignored: number
  last_24_hours: number
}

export interface DiagnosticListResult {
  items: ClientDiagnosticReport[]
  total: number
  limit: number
  offset: number
}

export const uploadClientDiagnostic = (payload: {
  metadata?: Record<string, string>
  archive_base64: string
}): Promise<ApiResponse<ClientDiagnosticReport>> => {
  return post<ApiResponse<ClientDiagnosticReport>>('/api/v1/client-diagnostics/upload', payload)
}

export const getClientDiagnostics = async (params?: {
  status?: DiagnosticStatus | ''
  severity?: DiagnosticSeverity | ''
  search?: string
  limit?: number
  offset?: number
}): Promise<ApiResponse<DiagnosticListResult>> => {
  const query = new URLSearchParams()
  if (params?.status) query.set('status', params.status)
  if (params?.severity) query.set('severity', params.severity)
  if (params?.search?.trim()) query.set('search', params.search.trim())
  query.set('limit', String(params?.limit || 50))
  query.set('offset', String(params?.offset || 0))
  return get<ApiResponse<DiagnosticListResult>>(`/api/v1/client-diagnostics?${query.toString()}`)
}

export const getClientDiagnosticStats = (): Promise<ApiResponse<DiagnosticStats>> => {
  return get<ApiResponse<DiagnosticStats>>('/api/v1/client-diagnostics/stats')
}

export const getClientDiagnostic = (reportId: number): Promise<ApiResponse<ClientDiagnosticReport>> => {
  return get<ApiResponse<ClientDiagnosticReport>>(`/api/v1/client-diagnostics/${reportId}`)
}

export const updateClientDiagnosticStatus = (
  reportId: number,
  status: DiagnosticStatus,
  note?: string,
): Promise<ApiResponse<ClientDiagnosticReport>> => {
  return post<ApiResponse<ClientDiagnosticReport>>(`/api/v1/client-diagnostics/${reportId}/status`, { status, note: note || '' })
}

export const deleteClientDiagnostic = (reportId: number): Promise<ApiResponse> => {
  return del(`/api/v1/client-diagnostics/${reportId}`)
}

export const downloadClientDiagnostic = async (
  reportId: number,
): Promise<{ success: boolean; blob?: Blob; filename?: string; message?: string }> => {
  const token = localStorage.getItem('auth_token') || localStorage.getItem('xr_token')
  const response = await fetch(`/api/v1/client-diagnostics/${reportId}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  const contentType = response.headers.get('content-type') || ''
  if (!response.ok || contentType.includes('application/json')) {
    let message = '诊断日志下载失败'
    try {
      const body = await response.json()
      message = body?.detail || body?.message || message
    } catch { /* keep the fallback message */ }
    return { success: false, message }
  }
  const disposition = response.headers.get('content-disposition') || ''
  const match = disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)["']?/i)
  return { success: true, blob: await response.blob(), filename: match ? decodeURIComponent(match[1]) : `diagnostic-${reportId}.zip` }
}
