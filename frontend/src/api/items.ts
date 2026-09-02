import { get, post, put, del } from '@/utils/request'
import type { Item, ApiResponse } from '@/types'

// API前缀
const ITEM_PREFIX = '/api/v1/items'

interface SyncedItemRow {
  id: number
  account_id: number
  account_name?: string
  external_id: string
  title: string
  description?: string | null
  price?: number | string | null
  stock?: number
  images?: string[] | null
  status?: string
  payload?: Record<string, unknown> | null
  created_at?: string
  updated_at?: string
}

function mapSyncedItem(row: SyncedItemRow): Item {
  const payload = row.payload || {}
  const rawPolishStatus = typeof payload.polish_status === 'string' ? payload.polish_status : ''
  const polishStatus: Item['polish_status'] = rawPolishStatus === 'submitted'
    || rawPolishStatus === 'platform_already_polished'
    || rawPolishStatus === 'verified'
    || rawPolishStatus === 'failed'
    ? rawPolishStatus
    : payload.is_polished === true ? 'submitted' : 'unknown'
  return {
    id: row.id,
    cookie_id: String(row.account_id),
    item_id: row.external_id,
    title: row.title,
    item_title: row.title,
    desc: row.description || undefined,
    item_detail: row.description || undefined,
    price: row.price === null || row.price === undefined ? undefined : String(row.price),
    item_price: row.price === null || row.price === undefined ? undefined : `¥${row.price}`,
    has_sku: Boolean(payload.has_sku),
    // 只有明确的读回核验才算“已擦亮”；接口 SUCCESS 仅展示为已提交。
    is_polished: payload.polish_verified === true || polishStatus === 'verified',
    polish_status: polishStatus,
    polish_status_message: typeof payload.polish_status_message === 'string' ? payload.polish_status_message : undefined,
    is_multi_spec: Boolean(payload.is_multi_spec),
    multi_quantity_delivery: Boolean(payload.multi_quantity_delivery),
    has_default_reply: Boolean(payload.has_default_reply),
    default_reply_enabled: Boolean(payload.default_reply_enabled),
    has_card: Boolean(payload.has_card),
    has_ai_prompt: Boolean(payload.has_ai_prompt),
    created_at: row.created_at,
    updated_at: row.updated_at,
  }
}

async function getSyncedItems(cookieId?: string, page = 1, pageSize = 100): Promise<{ items: Item[]; total: number }> {
  const result = await get<ApiResponse<{ items?: SyncedItemRow[]; total?: number }>>(
    cookieId
      ? `/api/v1/accounts/${cookieId}/contents?content_type=product&page=${page}&page_size=${pageSize}`
      : `${ITEM_PREFIX}?page=${page}&page_size=${pageSize}`,
  )
  const payload = result.data || {}
  const items = (payload.items || []).map(mapSyncedItem)
  return { items, total: payload.total ?? items.length }
}

export interface FetchItemsSummaryResponse extends ApiResponse {
  total_count?: number
  saved_count?: number
  account_count?: number
  success_account_count?: number
  failed_account_count?: number
  failed_accounts?: string[]
}

// 获取商品列表
export const getItems = async (cookieId?: string): Promise<{ success: boolean; data: Item[] }> => {
  const result = await getSyncedItems(cookieId)
  return { success: true, data: result.items }
}

// 商品筛选参数
export interface ItemFilterParams {
  keyword?: string | null            // 搜索关键字（商品ID/标题/详情）
  is_polished?: boolean | null      // 是否擦亮
  is_multi_spec?: boolean | null    // 多规格
  multi_quantity_delivery?: boolean | null  // 多数量发货
}

// 获取商品列表（分页）
export const getItemsPaginated = async (
  page: number = 1,
  pageSize: number = 20,
  cookieId?: string,
  filters?: ItemFilterParams
): Promise<{
  success: boolean
  data: Item[]
  total: number
  page: number
  page_size: number
  total_pages: number
}> => {
  const result = await getSyncedItems(cookieId, 1, 200)
  let items = result.items
  if (filters?.keyword?.trim()) {
    const keyword = filters.keyword.trim().toLowerCase()
    items = items.filter((item) => `${item.item_id} ${item.item_title || ''} ${item.item_detail || ''}`.toLowerCase().includes(keyword))
  }
  if (filters?.is_polished !== null && filters?.is_polished !== undefined) items = items.filter((item) => item.is_polished === filters.is_polished)
  if (filters?.is_multi_spec !== null && filters?.is_multi_spec !== undefined) items = items.filter((item) => Boolean(item.is_multi_spec) === filters.is_multi_spec)
  if (filters?.multi_quantity_delivery !== null && filters?.multi_quantity_delivery !== undefined) items = items.filter((item) => Boolean(item.multi_quantity_delivery) === filters.multi_quantity_delivery)
  const total = items.length
  return { success: true, data: items.slice((page - 1) * pageSize, page * pageSize), total, page, page_size: pageSize, total_pages: Math.ceil(total / pageSize) }
}

// ==================== 卡券关联商品选择弹窗 ====================

// 可选商品轻量项（仅选择场景所需字段）
export interface SelectableItem {
  item_id: string
  title?: string | null
  price?: string | null
}

// 获取全部匹配的可选商品轻量项（供「全选当前筛选结果」）
export const getAllSelectableItemKeys = (
  keyword: string = ''
): Promise<{ list: SelectableItem[]; total: number }> => {
  const params = new URLSearchParams()
  if (keyword && keyword.trim()) params.append('keyword', keyword.trim())
  const qs = params.toString()
  return get(`${ITEM_PREFIX}/selectable/all${qs ? `?${qs}` : ''}`)
}

// 获取卡券已关联商品的轻量详情（弹窗右侧「已选商品」展示用）
export const getItemsByCardId = (
  cardId: number
): Promise<{ list: SelectableItem[]; total: number }> => {
  return get(`${ITEM_PREFIX}/by-card/${cardId}`)
}

// 删除商品（账号可选）
// - 传入 cookieId：按账号删除
// - cookieId 为空（账号已删除的孤儿商品）：后端按商品ID删除
export const deleteItem = (cookieId: string | null | undefined, itemId: string): Promise<ApiResponse> => {
  return del(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}`)
}

// 批量删除商品
export const batchDeleteItems = async (ids: { cookie_id: string; item_id: string }[]): Promise<ApiResponse> => {
  const results = await Promise.all(ids.map((item) => deleteItem(item.cookie_id, item.item_id)))
  return { success: results.every((result) => result.success), message: `已删除 ${results.filter((result) => result.success).length} 个商品` }
}

// 批量下架商品（调用闲鱼接口，使用所选账号的Cookie）
export const batchOfflineItems = (cookieId: string, itemIds: string[]): Promise<ApiResponse> => {
  return post(`${ITEM_PREFIX}/account/${cookieId}/batch-offline`, { item_ids: itemIds })
}

// 批量删除闲鱼平台商品（本地商品记录保留）
export const batchDeleteXianyuItems = (cookieId: string, itemIds: string[]): Promise<ApiResponse> => {
  return post(`${ITEM_PREFIX}/account/${cookieId}/batch-delete-platform`, { item_ids: itemIds })
}

// 从账号获取商品（分页）
export const fetchItemsFromAccount = (cookieId: string, page?: number): Promise<ApiResponse> => {
  return post(`/api/v1/accounts/${cookieId}/sync`, { page_size: 20, max_pages: page || 1 })
}

// 获取账号所有页商品
export const fetchAllItemsFromAccount = (cookieId: string): Promise<FetchItemsSummaryResponse> => {
  return post<ApiResponse<{ total_count?: number; fetched_count?: number; product_count?: number; saved_count?: number; changed_count?: number }>>(`/api/v1/accounts/${cookieId}/sync`, { page_size: 20, max_pages: 100 }).then((result) => ({
    ...result,
    total_count: result.data?.total_count ?? result.data?.fetched_count ?? result.data?.product_count ?? 0,
    saved_count: result.data?.saved_count ?? 0,
  }))
}

// 获取当前权限范围内所有账号的所有商品
export const fetchAllItemsFromAccessibleAccounts = async (): Promise<FetchItemsSummaryResponse> => {
  const accounts = await get<ApiResponse<{ items?: Array<{ id: number }> }>>('/api/v1/accounts')
  const accountIds = accounts.data?.items || []
  const results = await Promise.all(accountIds.map((account) => fetchAllItemsFromAccount(String(account.id))))
  return {
    success: results.every((result) => result.success),
    message: results.find((result) => result.message)?.message || '同步完成',
    total_count: results.reduce((sum, result) => sum + (result.total_count || 0), 0),
    saved_count: results.reduce((sum, result) => sum + (result.saved_count || 0), 0),
    account_count: accountIds.length,
    success_account_count: results.filter((result) => result.success).length,
    failed_account_count: results.filter((result) => !result.success).length,
    failed_accounts: results.map((result, index) => result.success ? '' : String(accountIds[index].id)).filter(Boolean),
  }
}

// 更新商品
export const updateItem = (cookieId: string, itemId: string, data: Partial<Item>): Promise<ApiResponse> => {
  return put(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}`, data)
}

// 更新商品多数量发货状态
export const updateItemMultiQuantityDelivery = (cookieId: string, itemId: string, enabled: boolean): Promise<ApiResponse> => {
  return put(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/multi-quantity-delivery`, { multi_quantity_delivery: enabled })
}

// 更新商品多规格状态
export const updateItemMultiSpec = (cookieId: string, itemId: string, enabled: boolean): Promise<ApiResponse> => {
  return put(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/multi-spec`, { is_multi_spec: enabled })
}


// ==================== 商品默认回复 ====================

// 商品默认回复配置类型
export interface ItemDefaultReplyConfig {
  item_id: string
  reply_content: string
  reply_image: string
  enabled: boolean
  reply_once: boolean
  reply_type?: string  // text-文本，image-图片，api-接口
  api_url?: string
  api_timeout?: number
}

// 获取商品默认回复配置
export const getItemDefaultReply = (cookieId: string, itemId: string): Promise<ApiResponse<ItemDefaultReplyConfig>> => {
  return get(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/default-reply`)
}

// 保存商品默认回复配置
export const saveItemDefaultReply = (
  cookieId: string,
  itemId: string,
  data: { reply_content: string; reply_image?: string; enabled: boolean; reply_once: boolean; reply_type?: string; api_url?: string; api_timeout?: number }
): Promise<ApiResponse> => {
  return put(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/default-reply`, data)
}

// 上传商品默认回复图片
export const uploadItemDefaultReplyImage = async (
  cookieId: string,
  itemId: string,
  image: File
): Promise<{ success: boolean; image_url?: string; message?: string }> => {
  const formData = new FormData()
  formData.append('image', image)
  const result = await post<ApiResponse<{ image_url?: string }>>(`${ITEM_PREFIX}/${cookieId}/${encodeURIComponent(itemId)}/default-reply/upload-image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return { success: result.success, image_url: result.data?.image_url, message: result.message }
}

// 删除商品默认回复配置
export const deleteItemDefaultReply = (cookieId: string, itemId: string): Promise<ApiResponse> => {
  return del(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/default-reply`)
}

// 批量保存商品默认回复配置
export const batchSaveItemDefaultReply = (
  cookieId: string,
  data: { item_ids: string[]; reply_content: string; reply_image?: string; enabled: boolean; reply_once: boolean; reply_type?: string; api_url?: string; api_timeout?: number }
): Promise<ApiResponse> => {
  return Promise.all(data.item_ids.map((itemId) => saveItemDefaultReply(cookieId, itemId, data))).then((results) => ({
    success: results.every((result) => result.success),
    message: `已保存 ${results.filter((result) => result.success).length} 个商品默认回复`,
  }))
}

// 上传批量默认回复图片（使用第一个商品ID作为临时存储）
export const uploadBatchDefaultReplyImage = async (
  cookieId: string,
  image: File
): Promise<{ success: boolean; image_url?: string; message?: string }> => {
  const formData = new FormData()
  formData.append('image', image)
  const result = await post<ApiResponse<{ image_url?: string }>>(`${ITEM_PREFIX}/${cookieId}/batch-default-reply/upload-image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return { success: result.success, image_url: result.data?.image_url, message: result.message }
}

// 批量删除商品默认回复配置
export const batchDeleteItemDefaultReply = (
  cookieId: string,
  itemIds: string[]
): Promise<ApiResponse> => {
  return Promise.all(itemIds.map((itemId) => deleteItemDefaultReply(cookieId, itemId))).then((results) => ({
    success: results.every((result) => result.success),
    message: `已删除 ${results.filter((result) => result.success).length} 个商品默认回复`,
  }))
}


// ==================== 商品AI提示词 ====================

// 商品AI提示词配置类型
export interface ItemAiPromptConfig {
  item_id: string
  ai_prompt: string
}

// 获取商品AI提示词配置
export const getItemAiPrompt = (cookieId: string, itemId: string): Promise<ApiResponse<ItemAiPromptConfig>> => {
  return get(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/ai-prompt`)
}

// 保存商品AI提示词配置
export const saveItemAiPrompt = (
  cookieId: string,
  itemId: string,
  aiPrompt: string
): Promise<ApiResponse> => {
  return put(`${ITEM_PREFIX}/account/${cookieId}/${encodeURIComponent(itemId)}/ai-prompt`, { ai_prompt: aiPrompt })
}

// 批量删除商品AI提示词配置
export const batchDeleteItemAiPrompt = (
  cookieId: string,
  itemIds: string[]
): Promise<ApiResponse> => {
  return Promise.all(itemIds.map((itemId) => saveItemAiPrompt(cookieId, itemId, ''))).then((results) => ({
    success: results.every((result) => result.success),
    message: `已删除 ${results.filter((result) => result.success).length} 个商品 AI 提示词`,
  }))
}

// 批量保存商品AI提示词配置
export const batchSaveItemAiPrompt = (
  cookieId: string,
  data: { item_ids: string[]; ai_prompt: string }
): Promise<ApiResponse> => {
  return Promise.all(data.item_ids.map((itemId) => saveItemAiPrompt(cookieId, itemId, data.ai_prompt))).then((results) => ({
    success: results.every((result) => result.success),
    message: `已保存 ${results.filter((result) => result.success).length} 个商品 AI 提示词`,
  }))
}
