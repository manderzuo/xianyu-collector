/**
 * 商品监控分类 API
 *
 * 功能：
 * 1. 查询分类列表（普通用户仅见自己的分类，管理员可见全部）
 * 2. 新建、修改、删除分类（名称全局唯一，仅创建人或管理员可改删）
 */
import { get, post, put, del } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/product-monitor/categories'

export interface ListingMonitorCategory {
  id: number
  owner_id?: number | null
  name: string
  is_deleted?: boolean
  created_at?: string | null
  updated_at?: string | null
}

const categoryRows = (response: any): any[] => {
  const data = response?.data ?? response
  if (Array.isArray(data)) return data
  if (data && typeof data === 'object') {
    const rows = data.list ?? data.items ?? data.records
    return Array.isArray(rows) ? rows : (data.id !== undefined ? [data] : [])
  }
  return []
}

const normalizeCategory = (row: any): ListingMonitorCategory => ({
  id: Number(row.id ?? 0),
  owner_id: row.owner_id ?? row.payload?.owner_id ?? null,
  name: String(row.name ?? row.payload?.name ?? row.category_name ?? '未命名分类'),
  is_deleted: Boolean(row.is_deleted ?? row.payload?.is_deleted ?? false),
  created_at: row.created_at ?? null,
  updated_at: row.updated_at ?? row.created_at ?? null,
})

export const getListingMonitorCategories = (): Promise<ApiResponse<ListingMonitorCategory[]>> => {
  return get(`${PREFIX}`).then((response: any) => ({
    success: Boolean(response?.success ?? true),
    message: String(response?.message ?? '查询成功'),
    data: categoryRows(response).map(normalizeCategory),
  }))
}

export const createListingMonitorCategory = (
  name: string
): Promise<ApiResponse<ListingMonitorCategory>> => {
  return post(`${PREFIX}`, { name })
}

export const updateListingMonitorCategory = (
  id: number,
  name: string
): Promise<ApiResponse<ListingMonitorCategory>> => {
  return put(`${PREFIX}/${id}`, { name })
}

export const deleteListingMonitorCategory = (id: number): Promise<ApiResponse<null>> => {
  return del(`${PREFIX}/${id}`)
}
