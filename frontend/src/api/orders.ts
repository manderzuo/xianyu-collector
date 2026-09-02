import { get, del, post, put } from '@/utils/request'
import type { Order, ApiResponse } from '@/types'

const ORDER_PREFIX = '/api/v1/orders'

// 订单详情类型
export interface OrderDetail extends Order {
  spec_name?: string
  spec_value?: string
  receiver_name?: string
  receiver_phone?: string
  receiver_address?: string
}

// 分页响应类型
export interface OrderListResponse {
  success: boolean
  data: Order[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

interface CurrentOrderRow {
  id: number
  account_id: number
  order_no: string
  buyer_id?: string | null
  buyer_nick?: string | null
  product_id?: number | null
  item_id?: string | null
  item_external_id?: string | null
  item_title?: string | null
  buyer_fish_nick?: string | null
  quantity?: number
  spec_name?: string | null
  spec_value?: string | null
  amount: number | string
  status?: string
  delivery_method?: Order['delivery_method'] | null
  delivery_content?: string | null
  delivery_fail_reason?: string | null
  delivery_send_status?: Order['delivery_send_status']
  delivery_send_fail_reason?: string | null
  card_only_delivered?: boolean
  is_rated?: boolean
  is_red_flower?: boolean
  placed_at?: string | null
  created_at?: string
  updated_at?: string
}

function mapCurrentOrder(row: CurrentOrderRow): Order {
  const statusMap: Record<string, Order['status']> = {
    pending: 'pending_payment',
    paid: 'processing',
    processing: 'processing',
    pending_ship: 'pending_ship',
    processed: 'processed',
    shipped: 'shipped',
    completed: 'completed',
    refunding: 'refunding',
    refunded: 'refunded',
    cancelled: 'cancelled',
    unknown: 'unknown',
  }
  return {
    id: String(row.id),
    order_id: row.order_no,
    cookie_id: String(row.account_id),
    item_id: row.item_external_id || row.item_id || (row.product_id ? String(row.product_id) : '-'),
    item_title: row.item_title || undefined,
    buyer_id: row.buyer_id || '-',
    buyer_fish_nick: row.buyer_nick || row.buyer_fish_nick || undefined,
    sku_info: row.spec_name || row.spec_value ? `${row.spec_name || ''}: ${row.spec_value || ''}` : undefined,
    quantity: row.quantity || 1,
    amount: String(row.amount),
    status: statusMap[row.status || ''] || 'unknown',
    delivery_method: row.delivery_method || undefined,
    delivery_content: row.delivery_content || undefined,
    delivery_fail_reason: row.delivery_fail_reason || undefined,
    delivery_send_status: row.delivery_send_status || null,
    delivery_send_fail_reason: row.delivery_send_fail_reason || null,
    card_only_delivered: Boolean(row.card_only_delivered),
    is_bargain: false,
    is_rated: Boolean(row.is_rated),
    is_red_flower: Boolean(row.is_red_flower),
    is_unregistered: false,
    is_agent_order: false,
    source: 'local',
    placed_at: row.placed_at || row.created_at,
    created_at: row.created_at,
    updated_at: row.updated_at,
  }
}

// 手动发货响应类型
export interface ManualDeliveryResponse {
  success: boolean
  message: string
  data?: {
    order_no: string
    card_name: string
    card_type: string
  }
}

export interface FetchXianyuOrdersResponse {
  success: boolean
  message: string
  data?: {
    total_fetched: number
    new_inserted: number
    updated: number
    failed: number
    accounts_processed: number
    errors: string[]
    permission_limited_accounts?: number[]
  }
}

// 订单筛选参数
export interface OrderFilterParams {
  search?: string | null           // 搜索关键词（订单号、商品ID、买家ID）
  delivery_method?: string | null  // 发货方式：manual/auto/scheduled/none
  is_bargain?: boolean | null      // 是否小刀
  is_rated?: boolean | null        // 是否已评价
  start_date?: string | null       // 开始日期：YYYY-MM-DD
  end_date?: string | null         // 结束日期：YYYY-MM-DD
  delivery_send_status?: string | null  // 关联消息日志发送状态：sending/success/failed/unknown
}

// 获取订单列表（分页）
export const getOrders = (
  cookieId?: string,
  status?: string,
  page: number = 1,
  pageSize: number = 20,
  filters?: OrderFilterParams
): Promise<OrderListResponse> => {
  const params = new URLSearchParams()
  if (cookieId) params.append('cookie_id', cookieId)
  if (status) params.append('status', status)
  params.append('page', String(page))
  params.append('page_size', String(pageSize))
  
  if (filters) {
    if (filters.search) {
      params.append('search', filters.search)
    }
    if (filters.delivery_method !== null && filters.delivery_method !== undefined) {
      params.append('delivery_method', filters.delivery_method)
    }
    if (filters.is_bargain !== null && filters.is_bargain !== undefined) {
      params.append('is_bargain', String(filters.is_bargain))
    }
    if (filters.is_rated !== null && filters.is_rated !== undefined) {
      params.append('is_rated', String(filters.is_rated))
    }
    if (filters.start_date) {
      params.append('start_date', filters.start_date)
    }
    if (filters.end_date) {
      params.append('end_date', filters.end_date)
    }
    if (filters.delivery_send_status !== null && filters.delivery_send_status !== undefined && filters.delivery_send_status !== '') {
      params.append('delivery_send_status', filters.delivery_send_status)
    }
  }
  
  return get<ApiResponse<{ items?: CurrentOrderRow[]; total?: number; page?: number; page_size?: number }>>(`${ORDER_PREFIX}?${params.toString()}`).then((result) => {
    const payload = result.data || {}
    const data = (payload.items || []).map(mapCurrentOrder)
    const total = payload.total ?? data.length
    return { success: result.success, data, total, page: payload.page || page, page_size: payload.page_size || pageSize, total_pages: Math.ceil(total / pageSize) }
  })
}

// 获取订单详情
export const getOrderDetail = (orderNo: string, refresh = false): Promise<{ success: boolean; data: OrderDetail }> => {
  return get<ApiResponse<CurrentOrderRow>>(`${ORDER_PREFIX}/${orderNo}?refresh=${refresh}`).then((result) => ({ success: result.success, data: mapCurrentOrder(result.data || { id: 0, account_id: 0, order_no: orderNo, amount: 0 }) }))
}

// 删除订单
export const deleteOrder = (id: string): Promise<ApiResponse> => {
  return del(`${ORDER_PREFIX}/local/${id}`)
}

// 手动发货
export const manualDelivery = (orderNo: string): Promise<ManualDeliveryResponse> => {
  return post(`${ORDER_PREFIX}/manual-delivery`, { order_no: orderNo })
}

// 获取闲鱼订单并同步到数据库（单独设置10分钟超时）
export const noLogisticsDelivery = (orderNo: string): Promise<ManualDeliveryResponse> => {
  return post(`${ORDER_PREFIX}/no-logistics-delivery`, { order_no: orderNo })
}

export const cancelOrder = (orderNo: string): Promise<ApiResponse> => {
  return post(`${ORDER_PREFIX}/cancel`, { order_no: orderNo })
}

export const fetchXianyuOrders = (cookieId?: string): Promise<FetchXianyuOrdersResponse> => {
  return post(`${ORDER_PREFIX}/fetch-xianyu`, { cookie_id: cookieId || null }, { timeout: 600000 })
}

// 批量删除订单
export const batchDeleteOrders = (ids: number[]): Promise<ApiResponse> => {
  return post(`${ORDER_PREFIX}/batch-delete`, { ids })
}

// 更新订单状态
export const updateOrderStatus = async (_id: string, _status: string): Promise<ApiResponse> => {
  return put(`${ORDER_PREFIX}/${encodeURIComponent(_id)}/status`, { status: _status })
}
