import { get, post, put, del } from '@/utils/request'
import type { ApiResponse } from '@/types'

const CARD_PREFIX = '/api/v1/cards'

// 卡券类型定义
export interface CardData {
  id?: number
  item_id?: string  // 关联商品ID
  name: string
  type: 'api' | 'text' | 'data' | 'image'
  description?: string
  enabled?: boolean
  delay_seconds?: number
  delivery_count?: number  // 发货次数
  price?: string | null     // 对接价格
  is_dockable?: boolean    // 是否可对接
  fee_payer?: string | null  // 手续费支付方式：distributor/dealer
  min_price?: string | null  // 最低售价
  dock_visibility?: string | null  // 对接可见性：public-所有人可见，dealer_only-仅分销商可见
  is_multi_spec?: boolean
  spec_name?: string
  spec_value?: string
  api_config?: {
    url: string
    method: string
    timeout?: number
    headers?: string
    params?: string
    response_field?: string
  }
  text_content?: string
  data_content?: string
  image_url?: string
  image_urls?: string[]  // 多图片URL列表，最多3张
  created_at?: string
  updated_at?: string
  user_id?: number
  item_ids?: string[]  // 关联的商品ID列表（多对多）
  card_source?: 'own' | 'dock_l1' | 'dock_l2'  // 关联来源（从关联表返回）
  dock_record_id?: number | null  // 对接记录ID（从关联表返回）
}

// 卡券分页查询参数
export interface CardQueryParams {
  page?: number
  page_size?: number
  search?: string
  type?: string
}

// 卡券分页响应
export interface CardPaginatedResult {
  list: CardData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

// 获取卡券列表（分页）
export const getCards = async (params?: CardQueryParams): Promise<CardPaginatedResult> => {
  const query = new URLSearchParams()
  if (params?.page) query.set('page', String(params.page))
  if (params?.page_size) query.set('page_size', String(params.page_size))
  if (params?.search) query.set('search', params.search)
  if (params?.type) query.set('type', params.type)
  const qs = query.toString()
  const url = qs ? `${CARD_PREFIX}?${qs}` : CARD_PREFIX
  const response = await get<ApiResponse<CardPaginatedResult> | CardPaginatedResult>(url)
  const payload = (response as ApiResponse<CardPaginatedResult>)?.data && !Array.isArray((response as ApiResponse<CardPaginatedResult>).data)
    ? (response as ApiResponse<CardPaginatedResult>).data as CardPaginatedResult
    : response as CardPaginatedResult
  if ((response as ApiResponse)?.success === false) {
    throw new Error((response as ApiResponse).message || '加载卡券列表失败')
  }
  const list = (payload as any)?.list || (payload as any)?.items || []
  const total = Number((payload as any)?.total ?? list.length)
  const page = Number((payload as any)?.page ?? params?.page ?? 1)
  const pageSize = Number((payload as any)?.page_size ?? params?.page_size ?? 20)
  return { list, total, page, page_size: pageSize, total_pages: Number((payload as any)?.total_pages ?? (total ? Math.ceil(total / pageSize) : 0)) }
}

// 获取全部卡券（不分页，用于关联弹窗等场景）
// lite=1：仅返回列表所需轻字段（剔除卡密/文本/API配置/图片等大字段），
// 避免卡券过多时一次性传输超大 JSON 导致界面卡顿；完整内容用 getCard 按需获取
export const getAllCards = async (): Promise<CardData[]> => {
  const result = await getCards({ page: 1, page_size: 9999 })
  return result.list || []
}

// 获取单个卡券完整详情（用于列表中按需查看详情，补齐轻量列表未返回的大字段）
export const getCard = async (cardId: number): Promise<CardData> => {
  const response = await get<ApiResponse<CardData> | CardData>(`${CARD_PREFIX}/${cardId}`)
  return ((response as ApiResponse<CardData>).data || response) as CardData
}

// 商品关联卡券选择弹窗：可选卡券项（自有 + 对接 合并后的轻字段）
export interface SelectableCard {
  id?: number
  name: string
  type: string
  source: 'own' | 'dock_l1' | 'dock_l2'
  dock_name?: string | null
  dock_record_id?: number | null
  is_multi_spec?: boolean
  spec_name?: string
  spec_value?: string
  enabled?: boolean
  price?: string | null
  unique_key: string
}

// 可选卡券分页响应
export interface SelectablePaginatedResult {
  list: SelectableCard[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

// 合并分页获取商品可选卡券（自有 + 对接，服务端分页与搜索）
export const getSelectableCards = (
  itemId: string,
  page: number,
  pageSize: number,
  search: string = '',
): Promise<SelectablePaginatedResult> => {
  const q = new URLSearchParams()
  q.set('item_id', itemId)
  q.set('page', String(page))
  q.set('page_size', String(pageSize))
  if (search) q.set('search', search)
  return get<ApiResponse<SelectablePaginatedResult> | SelectablePaginatedResult>(`${CARD_PREFIX}/selectable?${q.toString()}`).then((response) => {
    const payload = (response as ApiResponse<SelectablePaginatedResult>).data || response as SelectablePaginatedResult
    const list = (payload as any)?.list || (payload as any)?.items || []
    const total = Number((payload as any)?.total ?? list.length)
    return { list, total, page, page_size: pageSize, total_pages: Number((payload as any)?.total_pages ?? (total ? Math.ceil(total / pageSize) : 0)) }
  })
}

// 获取全部匹配的可选卡券轻量项（供「全选当前筛选结果」）
export const getAllSelectableCardKeys = (
  search: string = '',
): Promise<{ list: SelectableCard[]; total: number }> => {
  const q = new URLSearchParams()
  if (search) q.set('search', search)
  const qs = q.toString()
  return get<ApiResponse<{ list?: SelectableCard[]; items?: SelectableCard[]; total?: number }> | { list?: SelectableCard[]; items?: SelectableCard[]; total?: number }>(`${CARD_PREFIX}/selectable/all${qs ? `?${qs}` : ''}`).then((response) => {
    const payload = (response as ApiResponse<any>).data || response as any
    const list = payload?.list || payload?.items || []
    return { list, total: Number(payload?.total ?? list.length) }
  })
}

// 按商品ID获取卡券列表
export const getCardsByItemId = async (itemId: string): Promise<{ success: boolean; data?: CardData[] }> => {
  const response = await get<ApiResponse<CardData[]> | CardData[]>(`${CARD_PREFIX}/item/${itemId}`)
  const data = (response as ApiResponse<CardData[]>).data || response
  return { success: Boolean((response as ApiResponse).success ?? true), data: Array.isArray(data) ? data : [] }
}

// 创建卡券
export const createCard = (data: Omit<CardData, 'id' | 'created_at' | 'updated_at' | 'user_id'>): Promise<ApiResponse<CardData>> => {
  return post(CARD_PREFIX, data)
}

// 更新卡券
export const updateCard = (cardId: string, data: Partial<CardData>): Promise<ApiResponse> => {
  return put(`${CARD_PREFIX}/${cardId}`, data)
}

// 删除卡券
export const deleteCard = (cardId: string): Promise<ApiResponse> => {
  return del(`${CARD_PREFIX}/${cardId}`)
}

// 批量删除卡券
export const batchDeleteCards = (cardIds: number[]): Promise<ApiResponse> => {
  return post(`${CARD_PREFIX}/batch-delete`, { ids: cardIds })
}

// 上传卡券图片
export const uploadCardImage = async (file: File): Promise<{ success: boolean; image_url?: string; message?: string }> => {
  const formData = new FormData()
  formData.append('image', file)
  const result = await post<ApiResponse<{ image_url?: string }>>(`${CARD_PREFIX}/upload-image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
  return { success: result.success, image_url: result.data?.image_url, message: result.message }
}

// 获取卡券关联的商品ID列表
export const getCardItemIds = (cardId: number): Promise<ApiResponse<{ item_ids: string[] }>> => {
  return get(`${CARD_PREFIX}/${cardId}/items`)
}

// 更新卡券关联的商品列表
export const updateCardItems = (cardId: number, itemIds: string[]): Promise<ApiResponse> => {
  return put(`${CARD_PREFIX}/${cardId}/items`, { item_ids: itemIds })
}

// 单条卡券关联信息
export interface CardRelationItem {
  card_id: number
  source: 'own' | 'dock_l1' | 'dock_l2'
  dock_record_id?: number | null
}

// 更新商品关联的卡券列表（先删旧关联再插新关联）
export const updateItemCards = (
  itemId: string,
  cardItems: CardRelationItem[],
): Promise<ApiResponse> => {
  return put(`${CARD_PREFIX}/item/${itemId}/cards`, {
    card_items: cardItems,
  })
}

// 批量清空商品的卡券关联关系（不删除卡券本身）
export const batchClearItemRelations = (itemIds: string[]): Promise<ApiResponse> => {
  return post(`${CARD_PREFIX}/batch-clear-item-relations`, { item_ids: itemIds })
}
