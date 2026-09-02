/**
 * 兜底下单账号配置 API（按分类）
 *
 * 功能：
 * 1. 列出当前用户已配置的兜底下单账号（按分类，含无分类那条）
 * 2. 新建/修改某个分类的兜底下单账号配置
 * 3. 删除某个分类的兜底下单账号配置
 *
 * 说明：定时下单/私信任务在监控任务无可用下单账号时，按 5 层链回退使用兜底账号。
 */
import { get, put, del } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/product-monitor/order-fallback-accounts'

export interface OrderFallbackAccountStatus {
  account_id: string
  valid: boolean
  reason?: string | null
}

export interface OrderFallbackAccountConfig {
  id: number | null
  owner_id?: number | null
  owner_username?: string | null
  category_id: number | null
  category_name?: string | null
  account_ids: string[]
  accounts: OrderFallbackAccountStatus[]
  created_at?: string | null
  updated_at?: string | null
}

const responseRows = (response: any): any[] => {
  const data = response?.data ?? response
  if (Array.isArray(data)) return data
  if (data && typeof data === 'object') {
    const rows = data.list ?? data.items ?? data.records
    return Array.isArray(rows) ? rows : (data.id !== undefined ? [data] : [])
  }
  return []
}

const normalizeConfig = (row: any): OrderFallbackAccountConfig => {
  const payload = row?.payload && typeof row.payload === 'object' ? row.payload : {}
  const accountIds = Array.isArray(row?.account_ids)
    ? row.account_ids
    : (Array.isArray(payload.account_ids) ? payload.account_ids : [])
  const accounts = Array.isArray(row?.accounts)
    ? row.accounts
    : accountIds.map((account_id: unknown) => ({ account_id: String(account_id), valid: true }))
  return {
    id: row?.id ?? null,
    owner_id: row?.owner_id ?? payload.owner_id ?? null,
    owner_username: row?.owner_username ?? payload.owner_username ?? null,
    category_id: row?.category_id ?? payload.category_id ?? null,
    category_name: row?.category_name ?? payload.category_name ?? null,
    account_ids: accountIds.map(String),
    accounts,
    created_at: row?.created_at ?? null,
    updated_at: row?.updated_at ?? row?.created_at ?? null,
  }
}

export const getOrderFallbackAccounts = (): Promise<ApiResponse<OrderFallbackAccountConfig[]>> => {
  return get(`${PREFIX}`).then((response: any) => ({
    success: Boolean(response?.success ?? true),
    message: String(response?.message ?? '查询成功'),
    data: responseRows(response).map(normalizeConfig),
  }))
}

export const saveOrderFallbackAccounts = (
  categoryId: number | null,
  accountIds: string[],
  ownerId?: number | null
): Promise<ApiResponse<OrderFallbackAccountConfig>> => {
  const body: Record<string, unknown> = { category_id: categoryId, account_ids: accountIds }
  // 管理员编辑其他用户配置时传 owner_id；普通用户不传，后端按当前用户处理
  if (ownerId != null) body.owner_id = ownerId
  return put(`${PREFIX}`, body).then((response: any) => ({
    success: Boolean(response?.success ?? true),
    message: String(response?.message ?? '保存成功'),
    data: normalizeConfig(response?.data ?? response),
  }))
}

export const deleteOrderFallbackAccounts = (
  categoryId: number | null,
  ownerId?: number | null
): Promise<ApiResponse<null>> => {
  const params = new URLSearchParams()
  if (categoryId != null) params.set('category_id', String(categoryId))
  if (ownerId != null) params.set('owner_id', String(ownerId))
  const query = params.toString()
  return del(`${PREFIX}${query ? `?${query}` : ''}`)
}
