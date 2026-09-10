import { get, post, put, del, patch } from '@/utils/request'
import type { AccountDetail, ApiResponse } from '@/types'

// API前缀
const COOKIE_PREFIX = '/api/v1/cookies'
const QR_LOGIN_PREFIX = '/api/v1/qr-login'
const PASSWORD_LOGIN_PREFIX = '/api/v1/password-login'
const AI_SETTINGS_PREFIX = '/api/v1/ai-reply-settings'

interface CurrentAccount {
  id: number
  account_name: string
  goofish_id?: string | null
  proxy?: string | null
  status?: string
  cookie_expire_at?: string | null
  created_at?: string | null
  online?: boolean
  ai_enabled?: boolean
  builtin_ai_reply_enabled?: boolean
  scheduled_redelivery?: boolean
  scheduled_rate?: boolean
  auto_polish?: boolean
  auto_confirm?: boolean
  confirm_before_send?: boolean
  send_before_confirm?: boolean
  only_send_card?: boolean
  auto_red_flower?: boolean
  ai_reply_block_ordered_users?: boolean
  delivery_disabled?: boolean
  delivery_disabled_reason?: string
  pause_duration?: number
  message_expire_time?: number
  reply_delay_seconds?: number
  keywordCount?: number
  filter_count?: number
  today_reply_count?: number
  username?: string
  has_password?: boolean
  show_browser?: boolean
}

function mapCurrentAccount(item: CurrentAccount): AccountDetail {
  const enabled = item.status === 'active'
  const hasPassword = item.has_password ?? false
  return {
    pk: item.id,
    // 重写版动作接口使用数据库账号主键；平台 ID 单独保留在 note 里展示。
    id: String(item.id),
    cookie: '',
    enabled,
    online: item.online ?? false,
    auto_confirm: item.auto_confirm ?? false,
    scheduled_redelivery: item.scheduled_redelivery ?? false,
    scheduled_rate: item.scheduled_rate ?? false,
    auto_polish: item.auto_polish ?? false,
    confirm_before_send: item.confirm_before_send ?? false,
    send_before_confirm: item.send_before_confirm ?? false,
    only_send_card: item.only_send_card ?? false,
    auto_red_flower: item.auto_red_flower ?? false,
    ai_reply_block_ordered_users: item.ai_reply_block_ordered_users ?? false,
    delivery_disabled: item.delivery_disabled ?? false,
    delivery_disabled_reason: item.delivery_disabled_reason || '',
    auto_close_order: false,
    delivery_only_card_after_close: false,
    delivery_disabled_excluded_item_ids: [],
    note: item.account_name,
    remark: item.account_name,
    show_browser: item.show_browser ?? false,
    disable_reason: enabled ? '' : '账号未启用',
    owner_id: undefined,
    created_at: item.created_at || undefined,
    updated_at: undefined,
    // 账号平台标识放入扩展字段，列表中仍然可核对真实闲鱼账号。
    username: item.username || undefined,
    login_password: undefined,
    has_password: hasPassword,
    pause_duration: item.pause_duration ?? 0,
    message_expire_time: item.message_expire_time ?? 0,
    reply_delay_seconds: item.reply_delay_seconds ?? 0,
    keywordCount: item.keywordCount ?? 0,
    filter_count: item.filter_count ?? 0,
    today_reply_count: item.today_reply_count ?? 0,
    aiEnabled: item.ai_enabled ?? false,
    builtinAiReplyEnabled: item.builtin_ai_reply_enabled ?? false,
    use_ai_reply: item.ai_enabled ?? false,
    use_default_reply: false,
  }
}

async function loadCurrentAccounts(): Promise<{ items: AccountDetail[]; total: number }> {
  const result = await get<ApiResponse<{ items?: CurrentAccount[]; total?: number }>>('/api/v1/accounts')
  const payload = result.data || {}
  const items = (payload.items || []).map(mapCurrentAccount)
  return { items, total: payload.total ?? items.length }
}

// 获取账号详情列表
export const getAccountDetails = async (): Promise<AccountDetail[]> => {
  const result = await loadCurrentAccounts()
  return result.items
}

// 账号筛选参数
export interface AccountFilterParams {
  status?: 'active' | 'inactive' | null  // 状态筛选
  ai_reply?: boolean | null              // AI回复开关
  scheduled_redelivery?: boolean | null  // 定时补发货
  scheduled_rate?: boolean | null        // 定时补评价
  auto_polish?: boolean | null           // 商品擦亮
  auto_confirm?: boolean | null          // 自动确认收货
  has_password?: boolean | null          // 是否配置密码
  online?: boolean | null                // 在线状态（true=在线/false=离线）
  disable_reason?: string | null         // 禁用原因关键词（模糊搜索）
  account_id?: string | null             // 账号ID关键词（模糊搜索）
  owner_username?: string | null         // 所属用户名关键词（模糊搜索，管理员用）
}

// 获取账号详情列表（分页）
export const getAccountDetailsPaginated = async (
  page: number = 1,
  pageSize: number = 20,
  filters?: AccountFilterParams
): Promise<{
  data: AccountDetail[]
  total: number
  page: number
  page_size: number
  total_pages: number
}> => {
  const result = await loadCurrentAccounts()
  let items = result.items
  if (filters?.status) items = items.filter((item) => filters.status === 'active' ? item.enabled : !item.enabled)
  const booleanFilters: Array<[keyof AccountFilterParams, keyof AccountDetail]> = [
    ['ai_reply', 'aiEnabled'],
    ['scheduled_redelivery', 'scheduled_redelivery'],
    ['scheduled_rate', 'scheduled_rate'],
    ['auto_polish', 'auto_polish'],
    ['auto_confirm', 'auto_confirm'],
    ['online', 'online'],
  ]
  for (const [filterKey, accountKey] of booleanFilters) {
    const expected = filters?.[filterKey]
    if (expected !== null && expected !== undefined) {
      items = items.filter((item) => Boolean(item[accountKey]) === expected)
    }
  }
  if (filters?.has_password !== null && filters?.has_password !== undefined) {
      items = items.filter((item) => Boolean(item.has_password) === filters.has_password)
  }
  if (filters?.disable_reason?.trim()) {
    const keyword = filters.disable_reason.trim()
    items = items.filter((item) => (item.disable_reason || '').includes(keyword))
  }
  if (filters?.account_id?.trim()) items = items.filter((item) => item.id.includes(filters.account_id!.trim()) || (item.username || '').includes(filters.account_id!.trim()))
  if (filters?.owner_username?.trim()) items = items.filter((item) => (item.owner_username || '').includes(filters.owner_username!.trim()))
  const total = items.length
  const start = (page - 1) * pageSize
  return { data: items.slice(start, start + pageSize), total, page, page_size: pageSize, total_pages: Math.ceil(total / pageSize) }
}

// 添加账号
export const addAccount = (data: { id: string; cookie: string }): Promise<ApiResponse> => {
  return post('/api/v1/accounts', { account_name: data.id, goofish_id: data.id, cookie: data.cookie })
}

// 更新账号 Cookie 值
export const updateAccountCookie = (id: string, value: string): Promise<ApiResponse> => {
  return put(`/api/v1/accounts/${id}`, { cookie: value })
}

// 更新账号启用/禁用状态
export const updateAccountStatus = (id: string, enabled: boolean): Promise<ApiResponse> => {
  return patch(`/api/v1/accounts/${id}/status?status=${enabled ? 'active' : 'inactive'}`)
}

export interface BatchAccountStatusResponseData {
  success_count: number
  failed_count: number
  success_ids: string[]
  failed_items: Array<{ account_id: string; message: string }>
}

export type BatchAccountOperationResponseData = BatchAccountStatusResponseData

export const updateAccountsStatusBatch = (accountIds: string[], enabled: boolean): Promise<ApiResponse<BatchAccountStatusResponseData>> => {
  return put(`${COOKIE_PREFIX}/status/batch`, { account_ids: accountIds, enabled })
}

export const closeAccountsNoticeBatch = (accountIds: string[]): Promise<ApiResponse<BatchAccountOperationResponseData>> => {
  return put(`${COOKIE_PREFIX}/close-notice/batch`, { account_ids: accountIds })
}

// 批量清除Token缓存并自动重启
export const clearTokenCacheBatch = (accountIds: string[]): Promise<ApiResponse<BatchAccountOperationResponseData>> => {
  return put(`${COOKIE_PREFIX}/clear-token-cache/batch`, { account_ids: accountIds })
}

// 批量账号续期（续期长登录token）
export const renewAccountLoginBatch = (accountIds: string[]): Promise<ApiResponse> => {
  return post(`${COOKIE_PREFIX}/renew-login`, accountIds)
}

// 更新账号备注
export const updateAccountRemark = (id: string, remark: string): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/remark`, { remark })
}

// 更新账号自动确认设置
export const updateAccountAutoConfirm = (id: string, autoConfirm: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/auto-confirm`, { auto_confirm: autoConfirm })
}

// 更新账号暂停时间
export const updateAccountPauseDuration = (id: string, pauseDuration: number): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/pause-duration`, { pause_duration: pauseDuration })
}

// 更新相同消息等待时间
export const updateAccountMessageExpireTime = (id: string, messageExpireTime: number): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/message-expire-time`, { message_expire_time: messageExpireTime })
}

// 更新自动回复延迟时间
export const updateAccountReplyDelay = (id: string, replyDelaySeconds: number): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/reply-delay`, { reply_delay_seconds: replyDelaySeconds })
}

// 更新定时补发货开关
export const updateAccountScheduledRedelivery = (id: string, scheduledRedelivery: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/scheduled-redelivery`, { scheduled_redelivery: scheduledRedelivery })
}

// 更新定时补评价开关
export const updateAccountScheduledRate = (id: string, scheduledRate: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/scheduled-rate`, { scheduled_rate: scheduledRate })
}

// 更新商品自动擦亮开关
export const updateAccountAutoPolish = (id: string, autoPolish: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/auto-polish`, { auto_polish: autoPolish })
}

// 更新发货成功再发卡券开关
export const updateAccountConfirmBeforeSend = (id: string, confirmBeforeSend: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/confirm-before-send`, { confirm_before_send: confirmBeforeSend })
}

// 更新卡券发送成功再确认发货开关
export const updateAccountSendBeforeConfirm = (id: string, sendBeforeConfirm: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/send-before-confirm`, { send_before_confirm: sendBeforeConfirm })
}

// 更新只发卡券、不确认发货开关
export const updateAccountOnlySendCard = (id: string, onlySendCard: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/only-send-card`, { only_send_card: onlySendCard })
}


// 更新自动求小红花开关
export const updateAccountAutoRedFlower = (id: string, autoRedFlower: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/auto-red-flower`, { auto_red_flower: autoRedFlower })
}

// 更新已下单用户禁止AI回复开关
export const updateAccountAiReplyBlockOrderedUsers = (id: string, aiReplyBlockOrderedUsers: boolean): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/ai-reply-block-ordered-users`, { ai_reply_block_ordered_users: aiReplyBlockOrderedUsers })
}

// ==================== 禁止发货规则 ====================

// 禁止发货规则项类型
export interface DeliveryBlockRuleItem {
  rule_code: string
  rule_name: string
  rule_description: string
  enabled: boolean
  priority: number
  block_reason: string
  auto_close_order: boolean
  only_card_after_close: boolean
  excluded_item_ids: string[]
  config: Record<string, any>
  default_config: Record<string, any>
}

// 获取账号的禁止发货规则列表
export const getDeliveryBlockRules = (id: string): Promise<ApiResponse<DeliveryBlockRuleItem[]>> => {
  return get(`${COOKIE_PREFIX}/${id}/delivery-block-rules`)
}

// 批量更新账号的禁止发货规则配置
export const updateDeliveryBlockRules = (
  id: string,
  rules: Array<{
    rule_code: string
    enabled: boolean
    priority: number
    block_reason: string | null
    auto_close_order: boolean
    only_card_after_close: boolean
    excluded_item_ids: string[]
    config: Record<string, any> | null
  }>,
): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/delivery-block-rules`, { rules })
}

// 获取所有可用的禁止发货规则类型
export const getAvailableDeliveryBlockRules = (): Promise<ApiResponse> => {
  return get(`${COOKIE_PREFIX}/delivery-block-rules/available`)
}

// 更新账号登录信息（用户名、密码、是否显示浏览器）
export const updateAccountLoginInfo = (
  id: string,
  data: { username?: string; login_password?: string; show_browser?: boolean }
): Promise<ApiResponse> => {
  return put(`${COOKIE_PREFIX}/${id}/login-info`, data)
}

// 删除账号
export const deleteAccount = (id: string): Promise<ApiResponse> => {
  return del(`/api/v1/accounts/${id}`)
}

// 账号密码登录
export const passwordLogin = (data: { account_id: string; account: string; password: string; show_browser?: boolean }): Promise<{
  success: boolean
  session_id?: string
  status?: string
  message?: string
}> => {
  return post(PASSWORD_LOGIN_PREFIX, data)
}

// 生成扫码登录二维码
export const generateQRLogin = async (): Promise<{ success: boolean; session_id?: string; qr_code_url?: string; message?: string }> => {
  const result = await post<{ success: boolean; message: string; data?: { session_id: string; qr_code_url: string } }>(`${QR_LOGIN_PREFIX}/generate`)
  // 从标准ApiResponse格式中提取数据
  return {
    success: result.success,
    message: result.message,
    session_id: result.data?.session_id,
    qr_code_url: result.data?.qr_code_url,
  }
}

// 检查扫码登录状态
export const checkQRLoginStatus = async (sessionId: string): Promise<{
  success: boolean
  status: 'pending' | 'scanned' | 'success' | 'failed' | 'expired' | 'cancelled' | 'verification_required' | 'processing' | 'already_processed' | 'error'
  message?: string
  face_qr_url?: string
  verification_url?: string
  account_info?: {
    account_id: string
    is_new_account: boolean
  }
}> => {
  const result = await get<{
    success: boolean
    message?: string
    data?: {
      status: string
      message?: string
      face_qr_url?: string
      verification_url?: string
      account_info?: { account_id: string; is_new_account: boolean }
    }
  }>(`${QR_LOGIN_PREFIX}/status/${sessionId}`)
  // 从标准ApiResponse格式中提取数据
  const status = result.data?.status || 'error'
  return {
    success: result.success,
    status: status as 'pending' | 'scanned' | 'success' | 'failed' | 'expired' | 'cancelled' | 'verification_required' | 'processing' | 'already_processed' | 'error',
    message: result.message || result.data?.message,
    face_qr_url: result.data?.face_qr_url,
    verification_url: result.data?.verification_url,
    account_info: result.data?.account_info,
  }
}

// 检查密码登录状态
export const checkPasswordLoginStatus = async (sessionId: string): Promise<{
  success: boolean
  status: 'pending' | 'processing' | 'success' | 'failed' | 'verification_required' | 'not_found'
  message?: string
  account_id?: string
  is_new_account?: boolean
  cookie_count?: number
  verification_url?: string
  screenshot_path?: string
  // 协议登录：触发人脸时的人脸二维码（base64 data-url）
  face_qr_url?: string
  error?: string
}> => {
  const result = await get<{
    status: string
    message?: string
    account_id?: string
    is_new_account?: boolean
    cookie_count?: number
    verification_url?: string
    screenshot_path?: string
    face_qr_url?: string
    error?: string
  }>(`${PASSWORD_LOGIN_PREFIX}/check/${sessionId}`)
  return {
    success: result.status === 'success',
    status: result.status as 'pending' | 'processing' | 'success' | 'failed' | 'verification_required' | 'not_found',
    message: result.message,
    account_id: result.account_id,
    is_new_account: result.is_new_account,
    cookie_count: result.cookie_count,
    verification_url: result.verification_url,
    screenshot_path: result.screenshot_path,
    face_qr_url: result.face_qr_url,
    error: result.error,
  }
}

// 取消密码登录会话
export const cancelPasswordLogin = (sessionId: string): Promise<{ success: boolean; message?: string }> => {
  return del(`${PASSWORD_LOGIN_PREFIX}/cancel/${sessionId}`)
}

// AI 服务商类型
export type AIProviderType = 'openai_compatible' | 'anthropic' | 'gemini' | 'dashscope_app'

// AI 服务商默认 API 地址
export const AI_PROVIDER_DEFAULT_BASE_URLS: Record<AIProviderType, string> = {
  openai_compatible: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  anthropic: 'https://api.anthropic.com',
  gemini: 'https://generativelanguage.googleapis.com',
  dashscope_app: 'https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion',
}

// AI 服务商可选项
export const AI_PROVIDER_OPTIONS: { value: AIProviderType; label: string; description: string }[] = [
  { value: 'openai_compatible', label: 'OpenAI兼容', description: '阿里云百炼、OpenAI、DeepSeek、Moonshot 等 OpenAI 协议接口' },
  { value: 'anthropic', label: 'Anthropic Claude', description: 'Anthropic 官方 Messages API' },
  { value: 'gemini', label: 'Google Gemini', description: 'Google AI Studio 官方接口' },
  { value: 'dashscope_app', label: 'DashScope应用', description: '阿里云百炼应用编排（包含 /apps/{app_id}）' },
]

// AI 回复设置接口 - 与后端 AIReplySettings 模型对应
export interface AIReplySettings {
  ai_enabled: boolean
  builtin_ai_reply_enabled?: boolean
  provider_type?: AIProviderType
  model_name?: string
  api_key?: string
  base_url?: string
  max_discount_percent?: number
  max_discount_amount?: number
  max_bargain_rounds?: number
  custom_prompts?: string
  ai_time_range_start?: string
  ai_time_range_end?: string
  // 兼容旧字段（前端内部使用）
  enabled?: boolean
}

export interface AIModelOption {
  id: string
  name: string
}

export interface AIModelListResponse {
  success: boolean
  message?: string
  data?: { models?: AIModelOption[] }
}

// 获取AI回复设置
export const getAIReplySettings = async (): Promise<AIReplySettings> => {
  const result = await get<ApiResponse<AIReplySettings>>(AI_SETTINGS_PREFIX)
  const data = result.data ?? ({} as AIReplySettings)
  return {
    ai_enabled: data.ai_enabled ?? false,
    builtin_ai_reply_enabled: data.builtin_ai_reply_enabled ?? false,
    enabled: data.ai_enabled ?? false,
    provider_type: data.provider_type,
    model_name: data.model_name,
    api_key: data.api_key,
    base_url: data.base_url,
    max_discount_percent: data.max_discount_percent,
    max_discount_amount: data.max_discount_amount,
    max_bargain_rounds: data.max_bargain_rounds,
    custom_prompts: data.custom_prompts,
    ai_time_range_start: data.ai_time_range_start,
    ai_time_range_end: data.ai_time_range_end,
  }
}

// 更新平台账号共享的 VIP 内置话术开关
export const updateBuiltinAIReply = (enabled: boolean): Promise<ApiResponse> => {
  return put(`${AI_SETTINGS_PREFIX}/builtin`, { enabled })
}

// 更新AI回复设置
export const updateAIReplySettings = (settings: Partial<AIReplySettings>): Promise<ApiResponse> => {
  const payload: Record<string, unknown> = {}
  if (settings.ai_enabled !== undefined || settings.enabled !== undefined) {
    payload.ai_enabled = settings.ai_enabled ?? settings.enabled ?? false
  }
  if (settings.provider_type !== undefined) payload.provider_type = settings.provider_type
  if (settings.model_name !== undefined) payload.model_name = settings.model_name
  if (settings.api_key !== undefined) payload.api_key = settings.api_key
  if (settings.base_url !== undefined) payload.base_url = settings.base_url
  if (settings.max_discount_percent !== undefined) payload.max_discount_percent = settings.max_discount_percent
  if (settings.max_discount_amount !== undefined) payload.max_discount_amount = settings.max_discount_amount
  if (settings.max_bargain_rounds !== undefined) payload.max_bargain_rounds = settings.max_bargain_rounds
  if (settings.custom_prompts !== undefined) payload.custom_prompts = settings.custom_prompts
  if (settings.ai_time_range_start !== undefined) payload.ai_time_range_start = settings.ai_time_range_start
  if (settings.ai_time_range_end !== undefined) payload.ai_time_range_end = settings.ai_time_range_end
  return put(AI_SETTINGS_PREFIX, { ai_enabled: payload.ai_enabled, ai_settings: payload })
}

// 获取当前平台账号的共享AI回复设置
export const getAllAIReplySettings = (): Promise<AIReplySettings> => {
  return get(AI_SETTINGS_PREFIX)
}

// 测试AI连接
export const testAIConnection = (): Promise<ApiResponse> => {
  return post(`${AI_SETTINGS_PREFIX}/test`)
}

// 按服务商手动获取模型列表
export const fetchAIModels = (params: {
  provider_type: AIProviderType
  base_url: string
  api_key: string
}): Promise<AIModelListResponse> => {
  return post(`${AI_SETTINGS_PREFIX}/models`, params)
}


// ==================== 代理配置 ====================

export interface ProxyConfig {
  proxy_type: 'none' | 'http' | 'https' | 'socks5'
  proxy_host?: string
  proxy_port?: number
  proxy_user?: string
  proxy_pass?: string
}

export interface ProxyConfigResponse {
  success: boolean
  message?: string
  data?: ProxyConfig
}

// 获取代理配置
export const getProxyConfig = (accountId: string): Promise<ProxyConfigResponse> => {
  return get<ApiResponse<{ proxy_config?: ProxyConfig }>>(`${COOKIE_PREFIX}/${accountId}/settings`).then((result) => ({
    success: result.success,
    message: result.message,
    data: result.data?.proxy_config,
  }))
}

// 更新代理配置
export const updateProxyConfig = (accountId: string, config: ProxyConfig): Promise<ProxyConfigResponse> => {
  return put<ApiResponse<{ proxy_config?: ProxyConfig }>>(`${COOKIE_PREFIX}/${accountId}/settings`, { proxy_config: config }).then((result) => ({
    success: result.success,
    message: result.message,
    data: result.data?.proxy_config,
  }))
}

// 清除代理配置
export const clearProxyConfig = (accountId: string): Promise<ProxyConfigResponse> => {
  return updateProxyConfig(accountId, { proxy_type: 'none' })
}

// ==================== 退款订单注销配置 ====================

const REFUND_CANCEL_PREFIX = '/api/v1/cookies'

export interface RefundCancelConfig {
  enabled: boolean      // 是否开启退款订单注销
  url?: string | null   // 注销接口请求URL
  timeout?: number      // 请求超时时间(秒)
}

export interface RefundCancelConfigResponse {
  success: boolean
  message?: string
  data?: RefundCancelConfig
}

// 获取退款订单注销配置
export const getRefundCancelConfig = (accountId: string): Promise<RefundCancelConfigResponse> => {
  return get(`${REFUND_CANCEL_PREFIX}/${accountId}/refund-cancel`)
}

// 更新退款订单注销配置
export const updateRefundCancelConfig = (accountId: string, config: RefundCancelConfig): Promise<RefundCancelConfigResponse> => {
  return put(`${REFUND_CANCEL_PREFIX}/${accountId}/refund-cancel`, config)
}

// ==================== 人脸验证相关 ====================
const FACE_VERIFICATION_PREFIX = '/api/v1/face-verification'

// 人脸验证截图信息
export interface FaceVerificationScreenshot {
  filename: string
  account_id: string
  path: string
  size: number
  created_time: number
  created_time_str: string
}

// 获取人脸验证截图
export const getFaceVerificationScreenshot = async (accountId: string): Promise<{
  success: boolean
  message?: string
  screenshot?: FaceVerificationScreenshot
}> => {
  const result = await get<{
    success: boolean
    message?: string
    data?: { screenshot?: FaceVerificationScreenshot }
  }>(`${FACE_VERIFICATION_PREFIX}/screenshot/${accountId}`)
  return {
    success: result.success,
    message: result.message,
    screenshot: result.data?.screenshot
  }
}

// 删除人脸验证截图
export const deleteFaceVerificationScreenshot = async (accountId: string): Promise<{
  success: boolean
  message?: string
  deleted_count?: number
}> => {
  const result = await del<{
    success: boolean
    message?: string
    data?: { deleted_count?: number }
  }>(`${FACE_VERIFICATION_PREFIX}/screenshot/${accountId}`)
  return {
    success: result.success,
    message: result.message,
    deleted_count: result.data?.deleted_count
  }
}

// ==================== 确认收货消息 ====================
const CONFIRM_RECEIPT_PREFIX = '/api/v1/cookies'

export interface ConfirmReceiptMessage {
  enabled: boolean
  message_content: string
  message_image: string
}

// 获取确认收货消息配置
export const getConfirmReceiptMessage = async (accountId: string): Promise<ConfirmReceiptMessage> => {
  const result = await get<ApiResponse<ConfirmReceiptMessage>>(`${CONFIRM_RECEIPT_PREFIX}/${accountId}/confirm-receipt`)
  return result.data || { enabled: false, message_content: '', message_image: '' }
}

// 更新确认收货消息配置
export const updateConfirmReceiptMessage = (
  accountId: string,
  data: ConfirmReceiptMessage
): Promise<ApiResponse> => {
  return put(`${CONFIRM_RECEIPT_PREFIX}/${accountId}/confirm-receipt`, data)
}

// 上传确认收货消息图片
export const uploadConfirmReceiptImage = (
  accountId: string,
  image: File
): Promise<{ success: boolean; image_url?: string; message?: string }> => {
  const formData = new FormData()
  formData.append('image', image)
  return post<ApiResponse<{ image_url?: string }>>(`${CONFIRM_RECEIPT_PREFIX}/${accountId}/confirm-receipt/upload-image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then((result) => ({
    success: result.success,
    image_url: result.data?.image_url,
    message: result.message,
  }))
}


// 获取账号统计数据
export interface AccountStats {
  total_accounts: number
  active_accounts: number
  total_keywords: number
  total_orders: number
  today_reply_count: number
  yesterday_reply_count: number
  account_limit: number | null
  used_account_count: number
  remaining_account_count: number | null
}

export const getAccountStats = async (): Promise<AccountStats> => {
  const response = await get<ApiResponse<AccountStats>>(`${COOKIE_PREFIX}/stats`)
  if (!response.success || !response.data) {
    throw new Error(response.message || '获取统计数据失败')
  }
  return response.data
}

// 14天订单金额趋势数据项
export interface OrderTrendItem {
  date: string
  amount: number
  count: number
}

// 获取近14天订单金额趋势
export const getOrderAmountTrend = async (): Promise<OrderTrendItem[]> => {
  const response = await get<ApiResponse<{ trend: OrderTrendItem[] }>>(`${COOKIE_PREFIX}/stats/order-trend`)
  if (!response.success || !response.data) {
    throw new Error(response.message || '获取订单金额趋势失败')
  }
  return response.data.trend
}

// 重写版账号详情：展示 Cookie 登录态、连接状态、同步结果及已同步内容。
export interface RewriteAccountContentDetail {
  account?: { id: number; account_name?: string; goofish_id?: string | null; status?: string; proxy?: string | null }
  login_state?: { has_cookie?: boolean; cookie_length?: number; cookie_records?: number; last_cookie_at?: string | null; expires_at?: string | null }
  connection?: {
    status?: string
    connection_state?: string
    is_connected?: boolean
    running?: boolean
    cookie_loaded?: boolean
    token_mode?: string
    last_error?: string
    action?: string
    updated_at?: string
  }
  content?: Record<string, number>
  sync?: { status?: string; last_started_at?: string | null; last_finished_at?: string | null; last_error?: string | null; products_count?: number; orders_count?: number; messages_count?: number; last_result?: Record<string, unknown> | null }
  recent_products?: Array<{ id?: number; external_id?: string; title?: string; price?: number | null; status?: string; synced_at?: string | null }>
}

export const getRewriteAccountContentDetail = (accountId: string | number): Promise<ApiResponse<RewriteAccountContentDetail>> => {
  return get(`/api/v1/accounts/${accountId}/detail`)
}

export const syncRewriteAccountContent = (accountId: string | number): Promise<ApiResponse<Record<string, unknown>>> => {
  return post(`/api/v1/accounts/${accountId}/sync`, { page_size: 20, max_pages: 100 })
}

// 导出账号数据为Excel
export const exportAccountsExcel = async (params: {
  account_ids?: string[]
  status?: string | null
  account_id?: string | null
  has_password?: boolean | null
}): Promise<Blob> => {
  const token = localStorage.getItem('auth_token')
  const response = await fetch(`${COOKIE_PREFIX}/export`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
    body: JSON.stringify({
      account_ids: params.account_ids || null,
      status: params.status || null,
      account_id: params.account_id || null,
      has_password: params.has_password ?? null,
    }),
  })
  if (!response.ok) {
    const errorText = await response.text()
    throw new Error(errorText || '导出失败')
  }
  return response.blob()
}

// 导入账号数据（从Excel文件）
export const importAccountsExcel = async (file: File, enableAll: boolean): Promise<{
  success: boolean
  message: string
  data?: {
    inserted: number
    updated: number
    started: number
    failed: number
    errors: string[]
  }
}> => {
  const token = localStorage.getItem('auth_token')
  const formData = new FormData()
  formData.append('file', file)
  formData.append('enable_all', String(enableAll))
  const response = await fetch(`${COOKIE_PREFIX}/import`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
    },
    body: formData,
  })
  return response.json()
}
