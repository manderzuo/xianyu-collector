/**
 * 商品发布 API 接口层
 *
 * 功能：
 * 1. 素材库 CRUD
 * 2. 单品发布 / 批量发布
 * 3. 发布日志查询
 */
import { get, post, put, del } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/product-publish'

// ==================== 类型定义 ====================

export interface PlatformMaterialAttribute {
  property_id?: string | null
  property_name?: string | null
  value_id?: string | null
  value_name?: string | null
  text?: string | null
  properties?: string | null
}

export interface PlatformCategoryPathItem {
  id: string
  name: string
}

export interface PlatformCategoryCandidate {
  cat_id?: string | null
  cat_name?: string | null
  channel_cat_id?: string | null
  channel_cat_name?: string | null
  leaf_id?: string | null
  tb_cat_id?: string | null
  path: PlatformCategoryPathItem[]
  score?: number | null
  is_selected?: boolean
  is_service_category?: boolean
  /** 由后端按真实 APP 编辑结果标注的履约模式，不是闲鱼官方字段。 */
  inventory_mode?: 'verified' | 'candidate' | 'service' | 'single'
  inventory_label?: string
  inventory_reason?: string
  inventory_price_unit?: string
}

export interface PlatformCategoryPropertyOption {
  property_id: string
  property_name: string
  value_id?: string | null
  value_name: string
  channel_cat_id?: string | null
  tb_cat_id?: string | null
}

export interface PlatformCategoryProperty {
  property_id: string
  property_name: string
  input_word?: string | null
  is_multiple?: boolean
  is_decisive_property?: boolean
  options: PlatformCategoryPropertyOption[]
}

export interface PlatformCategoryCardValue {
  catId?: string | null
  catName?: string | null
  channelCatId?: string | null
  channelCatName?: string | null
  tbCatId?: string | null
  isClicked?: string | null
  isUserClick?: string | null
  [key: string]: unknown
}

export interface PlatformCategoryCardData {
  propertyId?: string | null
  propertyName?: string | null
  valuesList?: PlatformCategoryCardValue[]
  [key: string]: unknown
}

export interface PlatformCategoryRecommendData {
  candidates: PlatformCategoryCandidate[]
  properties: PlatformCategoryProperty[]
  card_list?: PlatformCategoryCardData[]
  account_id?: string
}

export interface MaterialVideo {
  url: string
  path?: string | null
  name?: string | null
  size?: number | null
  file_id?: string | null
  width?: number | null
  height?: number | null
  duration_ms?: number | null
}

export interface PublishSpecificationValue {
  name: string
  image?: string | null
}

export interface PublishSpecification {
  name: string
  values: PublishSpecificationValue[]
  support_image?: boolean
}

export interface PublishSkuRow {
  specs: Record<string, string>
  price: number
  stock: number
}

export interface ProductMaterial {
  id: number
  user_id: number
  username?: string  // 管理员场景返回
  title: string
  description: string
  price: number
  original_price?: number | null
  publish_type?: 'item' | 'service'
  category?: string | null
  platform_category_id?: string | null
  platform_category_name?: string | null
  platform_channel_category_id?: string | null
  platform_channel_category_name?: string | null
  platform_leaf_id?: string | null
  platform_tb_category_id?: string | null
  platform_category_path: PlatformCategoryPathItem[]
  platform_card_list?: PlatformCategoryCardData[]
  is_service_category?: boolean
  inventory_mode?: 'verified' | 'candidate' | 'service' | 'single'
  inventory_label?: string
  inventory_reason?: string
  inventory_price_unit?: string
  platform_attributes: PlatformMaterialAttribute[]
  category_source: 'manual' | 'recommendation'
  category_confidence?: number | null
  images: string[]
  videos: MaterialVideo[]
  specifications: PublishSpecification[]
  sku_rows: PublishSkuRow[]
  quantity: number
  delivery_method: 'express' | 'pickup'
  shipping_method: 'free' | 'distance' | 'fixed' | 'template' | 'none'
  support_pickup: boolean
  postage: number
  address?: string | null
  address_expected_text?: string | null
  brand?: string | null
  condition: string
  remark?: string | null
  created_at: string
  updated_at: string
}

export interface MaterialCreateParams {
  title: string
  description: string
  price: number
  original_price?: number | null
  publish_type?: 'item' | 'service'
  category?: string | null
  platform_category_id?: string | null
  platform_category_name?: string | null
  platform_channel_category_id?: string | null
  platform_channel_category_name?: string | null
  platform_leaf_id?: string | null
  platform_tb_category_id?: string | null
  platform_category_path?: PlatformCategoryPathItem[]
  platform_card_list?: PlatformCategoryCardData[]
  is_service_category?: boolean
  inventory_mode?: 'verified' | 'candidate' | 'service' | 'single'
  inventory_label?: string
  inventory_reason?: string
  inventory_price_unit?: string
  platform_attributes?: PlatformMaterialAttribute[]
  category_source?: 'manual' | 'recommendation'
  category_confidence?: number | null
  images: string[]
  videos?: MaterialVideo[]
  specifications?: PublishSpecification[]
  sku_rows?: PublishSkuRow[]
  quantity?: number
  delivery_method?: 'express' | 'pickup'
  shipping_method?: 'free' | 'distance' | 'fixed' | 'template' | 'none'
  support_pickup?: boolean
  postage?: number
  address?: string | null
  address_expected_text?: string | null
  brand?: string | null
  condition?: string
  remark?: string | null
}

export interface MaterialListResponse {
  success: boolean
  message: string
  data: {
    list: ProductMaterial[]
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}

export interface PublishLog {
  id: number
  user_id: number
  username?: string  // 管理员场景返回
  account_id: string
  title: string
  description?: string
  price?: string
  material_id?: number | null
  batch_id?: string | null
  status: 'pending' | 'publishing' | 'success' | 'failed'
  item_url?: string | null
  item_id?: string | null
  error_message?: string | null
  resolved_address_id?: number | null
  resolved_address_text?: string | null
  address_source?: 'material' | 'account_pool' | 'global_pool' | 'personal_pool' | null
  created_at: string
  updated_at: string
}

export interface PublishLogListResponse {
  success: boolean
  message: string
  data: {
    list: PublishLog[]
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}

export interface BatchAccountStatus {
  account_id: string
  total: number
  success: number
  failed: number
  publishing: number
  pending: number
  sync_status: 'pending' | 'running' | 'success' | 'failed' | 'skipped' | 'unknown'
  sync_message: string
  sync_total_count: number
  sync_saved_count: number
}

export interface BatchStatusResponse {
  success: boolean
  message: string
  data: {
    batch_id: string
    total: number
    success: number
    failed: number
    publishing: number
    pending: number
    finished: boolean
    account_statuses: BatchAccountStatus[]
  }
}

export interface PublishSingleResponseData {
  item_url?: string | null
  item_id?: string | null
  log_id?: number
  sync_status?: 'success' | 'failed' | 'skipped'
  sync_message?: string | null
  sync_total_count?: number
  sync_saved_count?: number
  requires_app?: boolean
  draft_id?: string | null
  qr_content?: string | null
}

export type PublishSingleResponse = ApiResponse<PublishSingleResponseData>

export interface PublishCommissionConfig {
  title: string
  default_title: string
  tips: string
  percent: string
  max_commission: string
  tip_url: string
}

export interface PublishAccountCapability {
  account_id: string
  is_fish_shop: boolean
  support_sku_or_inventory: boolean
  commission_config: PublishCommissionConfig
}

export interface PublishBatchResponseData {
  batch_id: string
  total: number
}

export type PublishBatchResponse = ApiResponse<PublishBatchResponseData>

type RawRecord = Record<string, any>

const asRecord = (value: unknown): RawRecord => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as RawRecord : {}
)

const listFromResponse = (response: unknown): RawRecord[] => {
  const root = asRecord(response)
  const data = root.data ?? root
  if (Array.isArray(data)) return data as RawRecord[]
  const payload = asRecord(data)
  const rows = payload.list ?? payload.items ?? payload.records ?? []
  return Array.isArray(rows) ? rows as RawRecord[] : []
}

const normalizeMaterial = (raw: RawRecord): ProductMaterial => {
  const metadata = asRecord(raw.metadata_json ?? raw.metadata)
  const value = (key: string, fallback?: unknown) => raw[key] !== undefined && raw[key] !== null ? raw[key] : (metadata[key] ?? fallback)
  const imagesValue = value('images', raw.url ? [raw.url] : [])
  const videosValue = value('videos', [])
  const pathValue = value('platform_category_path', [])
  return {
    id: Number(raw.id ?? 0),
    user_id: Number(raw.user_id ?? raw.owner_id ?? 0),
    username: raw.username,
    title: String(value('title', raw.name || '未命名素材')),
    description: String(value('description', '')),
    price: Number(value('price', 0) || 0),
    original_price: value('original_price') == null ? null : Number(value('original_price')),
    publish_type: value('publish_type', value('is_service_category', false) ? 'service' : 'item') as ProductMaterial['publish_type'],
    category: value('category') == null ? null : String(value('category')),
    platform_category_id: value('platform_category_id') == null ? null : String(value('platform_category_id')),
    platform_category_name: value('platform_category_name') == null ? null : String(value('platform_category_name')),
    platform_channel_category_id: value('platform_channel_category_id') == null ? null : String(value('platform_channel_category_id')),
    platform_channel_category_name: value('platform_channel_category_name') == null ? null : String(value('platform_channel_category_name')),
    platform_leaf_id: value('platform_leaf_id') == null ? null : String(value('platform_leaf_id')),
    platform_tb_category_id: value('platform_tb_category_id') == null ? null : String(value('platform_tb_category_id')),
    platform_category_path: Array.isArray(pathValue) ? pathValue.map((item) => {
      const entry = asRecord(item)
      return { id: String(entry.id ?? ''), name: String(entry.name ?? '') }
    }) : [],
    platform_card_list: Array.isArray(value('platform_card_list', [])) ? value('platform_card_list', []) as PlatformCategoryCardData[] : [],
    is_service_category: Boolean(value('is_service_category', false)),
    inventory_mode: value('inventory_mode') as ProductMaterial['inventory_mode'],
    inventory_label: value('inventory_label') == null ? undefined : String(value('inventory_label')),
    inventory_reason: value('inventory_reason') == null ? undefined : String(value('inventory_reason')),
    inventory_price_unit: value('inventory_price_unit') == null ? undefined : String(value('inventory_price_unit')),
    platform_attributes: Array.isArray(value('platform_attributes', [])) ? value('platform_attributes', []) as PlatformMaterialAttribute[] : [],
    category_source: value('category_source', 'manual') as 'manual' | 'recommendation',
    category_confidence: value('category_confidence') == null ? null : Number(value('category_confidence')),
    images: Array.isArray(imagesValue) ? imagesValue.map(String) : [],
    videos: Array.isArray(videosValue) ? videosValue as MaterialVideo[] : [],
    specifications: Array.isArray(value('specifications', [])) ? value('specifications', []) as PublishSpecification[] : [],
    sku_rows: Array.isArray(value('sku_rows', [])) ? value('sku_rows', []) as PublishSkuRow[] : [],
    quantity: Number(value('quantity', 1) || 1),
    delivery_method: value('delivery_method', 'express') as 'express' | 'pickup',
    shipping_method: value('shipping_method', 'free') as 'free' | 'distance' | 'fixed' | 'template' | 'none',
    support_pickup: Boolean(value('support_pickup', false)),
    postage: Number(value('postage', 0) || 0),
    address: value('address') == null ? null : String(value('address')),
    address_expected_text: value('address_expected_text') == null ? null : String(value('address_expected_text')),
    brand: value('brand') == null ? null : String(value('brand')),
    condition: String(value('condition', '全新')),
    remark: value('remark') == null ? null : String(value('remark')),
    created_at: String(raw.created_at ?? metadata.created_at ?? ''),
    updated_at: String(raw.updated_at ?? metadata.updated_at ?? raw.created_at ?? ''),
  }
}

/** 根据商品标题和描述推荐闲鱼平台分类。 */
export const recommendPlatformCategory = (params: {
  title: string
  description: string
  account_id?: string
  current_card_list?: PlatformCategoryCardData[]
  selected_list?: Record<string, unknown>[]
  cat_id?: string
  cat_name?: string
  channel_cat_id?: string
}): Promise<ApiResponse<PlatformCategoryRecommendData>> =>
  post(`${PREFIX}/category/recommend`, params)

// ==================== 素材库接口 ====================

/** 创建素材 */
export const createMaterial = (params: MaterialCreateParams): Promise<ApiResponse> =>
  post(`${PREFIX}/materials`, params)

/** 分页查询素材列表 */
export const getMaterials = (
  page = 1,
  pageSize = 20,
  filters?: { title?: string; category?: string; condition?: string; platform_category_id?: string }
): Promise<MaterialListResponse> => {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  if (filters?.title) params.append('title', filters.title)
  if (filters?.category) params.append('category', filters.category)
  if (filters?.condition) params.append('condition', filters.condition)
  if (filters?.platform_category_id) params.append('platform_category_id', filters.platform_category_id)
  return get(`${PREFIX}/materials?${params}`).then((response: any) => {
    const root = asRecord(response)
    const data = asRecord(root.data ?? root)
    const list = listFromResponse(response).map(normalizeMaterial)
    const total = Number(data.total ?? list.length)
    return {
      success: Boolean(root.success ?? true),
      message: String(root.message ?? '查询成功'),
      data: {
        list,
        total,
        page: Number(data.page ?? page),
        page_size: Number(data.page_size ?? pageSize),
        total_pages: Number(data.total_pages ?? Math.max(1, Math.ceil(total / pageSize))),
      },
    }
  })
}

/** 获取单条素材详情 */
export const getMaterial = (id: number): Promise<ApiResponse<ProductMaterial>> =>
  get(`${PREFIX}/materials/${id}`)

/** 更新素材 */
export const updateMaterial = (
  id: number,
  params: Partial<MaterialCreateParams>
): Promise<ApiResponse> => put(`${PREFIX}/materials/${id}`, params)

/** 删除素材 */
export const deleteMaterial = (id: number): Promise<ApiResponse> =>
  del(`${PREFIX}/materials/${id}`)

/** 批量删除素材 */
export const batchDeleteMaterials = (ids: number[]): Promise<ApiResponse> =>
  post(`${PREFIX}/materials/batch-delete`, { ids })

// ==================== 发布接口 ====================

/** 查询账号是否开通鱼小铺及其发布能力。 */
export const getPublishAccountCapability = (
  accountId: string,
): Promise<ApiResponse<PublishAccountCapability>> =>
  get(`${PREFIX}/accounts/${encodeURIComponent(accountId)}/capability`)

/** 单品发布（同步调用闲鱼发布接口） */
export const publishSingle = (params: {
  account_id: string
  title: string
  description: string
  price: number
  original_price?: number | null
  publish_type?: 'item' | 'service'
  category?: string
  platform_category_id?: string | null
  platform_category_name?: string | null
  platform_channel_category_id?: string | null
  platform_channel_category_name?: string | null
  platform_leaf_id?: string | null
  platform_tb_category_id?: string | null
  platform_category_path?: PlatformCategoryPathItem[]
  platform_card_list?: PlatformCategoryCardData[]
  is_service_category?: boolean
  inventory_mode?: 'verified' | 'candidate' | 'service' | 'single'
  inventory_label?: string
  inventory_reason?: string
  inventory_price_unit?: string
  platform_attributes?: PlatformMaterialAttribute[]
  category_source?: 'manual' | 'recommendation'
  category_confidence?: number | null
  images: string[]        // 本地绝对路径，由 uploadProductImages 返回
  videos?: MaterialVideo[]
  quantity?: number
  specifications?: PublishSpecification[]
  sku_rows?: PublishSkuRow[]
  stock?: number
  address?: string
  address_expected_text?: string
  delivery_method?: string
  shipping_method?: 'free' | 'distance' | 'fixed' | 'template' | 'none'
  support_pickup?: boolean
  postage?: number
  brand?: string
  condition?: string
}): Promise<PublishSingleResponse> =>
  post(`${PREFIX}/publish/single`, params, { timeout: 90000 })

/** 批量发布（异步，立即返回 batch_id） */
export const publishBatch = (params: {
  account_ids: string[]
  material_ids: number[]
}): Promise<PublishBatchResponse> => post(`${PREFIX}/publish/batch`, params)

/** 查询批量发布任务状态 */
export const getBatchStatus = (batchId: string): Promise<BatchStatusResponse> =>
  get(`${PREFIX}/publish/batch/${batchId}/status`)

// ==================== 图片上传 ====================

/** 上传商品图片（返回本地路径供 Playwright 使用 + URL 供预览）
 *  注意：不要手动设置 Content-Type，axios 会自动添加正确的 multipart boundary
 */
export const uploadProductImages = async (files: File[]): Promise<{
  success: boolean
  message: string
  data?: { paths: string[]; urls: string[] }
}> => {
  const formData = new FormData()
  files.forEach(f => formData.append('files', f))
  return post(`${PREFIX}/upload/images`, formData)
}

/** 上传商品视频，返回本地路径和预览地址。 */
export const uploadProductVideos = async (files: File[]): Promise<{
  success: boolean
  message: string
  data?: { videos: MaterialVideo[]; paths: string[]; urls: string[] }
}> => {
  const formData = new FormData()
  files.forEach((file) => formData.append('files', file))
  return post(`${PREFIX}/upload/videos`, formData)
}

/** 分页查询发布日志 */
export const getPublishLogs = (
  page = 1,
  pageSize = 20,
  accountId?: string,
  status?: string
): Promise<PublishLogListResponse> => {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  if (accountId) params.append('account_id', accountId)
  if (status) params.append('status', status)
  return get(`${PREFIX}/logs?${params}`).then((response: any) => {
    const root = asRecord(response)
    const data = asRecord(root.data ?? root)
    const list = listFromResponse(response).map((raw) => ({
      id: Number(raw.id ?? 0),
      user_id: Number(raw.user_id ?? raw.owner_id ?? 0),
      username: raw.username,
      account_id: String(raw.account_id ?? raw.payload?.account_id ?? '-'),
      title: String(raw.title ?? raw.payload?.title ?? raw.name ?? '未命名商品'),
      description: raw.description ?? raw.payload?.description,
      price: raw.price == null && raw.payload?.price == null ? undefined : String(raw.price ?? raw.payload?.price),
      material_id: raw.material_id ?? raw.payload?.material_id ?? null,
      batch_id: raw.batch_id ?? raw.payload?.batch_id ?? null,
      status: (raw.status === 'queued' ? 'pending' : raw.status ?? raw.payload?.status ?? 'pending') as PublishLog['status'],
      item_url: raw.item_url ?? raw.payload?.item_url ?? null,
      item_id: raw.item_id ?? raw.payload?.item_id ?? null,
      error_message: raw.error_message ?? raw.message ?? raw.note ?? null,
      resolved_address_id: raw.resolved_address_id ?? null,
      resolved_address_text: raw.resolved_address_text ?? null,
      address_source: raw.address_source ?? null,
      created_at: String(raw.created_at ?? ''),
      updated_at: String(raw.updated_at ?? raw.created_at ?? ''),
    }))
    const total = Number(data.total ?? list.length)
    return {
      success: Boolean(root.success ?? true),
      message: String(root.message ?? '查询成功'),
      data: {
        list,
        total,
        page: Number(data.page ?? page),
        page_size: Number(data.page_size ?? pageSize),
        total_pages: Number(data.total_pages ?? Math.max(1, Math.ceil(total / pageSize))),
      },
    }
  })
}

export const clearPublishLogs = async (): Promise<{ success: boolean; message: string }> => {
  return del<{ success: boolean; message: string }>(`${PREFIX}/logs/clear`)
}
