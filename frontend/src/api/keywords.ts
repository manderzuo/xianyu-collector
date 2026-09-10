import { del, get, post, put } from '@/utils/request'
import type { Keyword, ApiResponse } from '@/types'

const KEYWORD_PREFIX = '/api/v1/keywords-with-item-id'

// 获取关键词列表（包含 item_id 和 type）
export const getKeywords = async (cookieId?: string): Promise<Keyword[]> => {
  // 如果没有传cookieId，查询所有账号的关键词
  const result = await get<ApiResponse<{ items?: Keyword[]; list?: Keyword[] }> | Keyword[]>(cookieId ? `${KEYWORD_PREFIX}/${cookieId}` : KEYWORD_PREFIX)
  if (Array.isArray(result)) return result
  const payload = result.data
  if (Array.isArray(payload)) return payload
  return payload?.items || payload?.list || []
}

// 保存关键词列表（替换整个列表）
// 后端接口: POST /keywords-with-item-id/{cid}
// 请求体: { keywords: [{ keyword, reply, item_id }, ...] }
export const saveKeywords = (cookieId: string, keywords: Keyword[]): Promise<ApiResponse> => {
  // 只发送文本类型的关键词，图片类型通过单独接口处理
  const textKeywords = keywords
    .filter(k => k.type !== 'image')
    .map(k => ({
      keyword: k.keyword,
      reply: k.reply || '',
      item_id: k.item_id || '',
      priority: k.priority ?? 0,
      match_mode: k.match_mode || 'contains',
      conversation_stage: k.conversation_stage || '',
      enabled: k.enabled !== false,
      needs_human: k.needs_human === true,
      approval_status: k.approval_status || 'approved',
    }))
  return post(`${KEYWORD_PREFIX}/${cookieId}`, { keywords: textKeywords })
}

// 添加关键词（先获取列表，添加后保存）
export const addKeyword = async (cookieId: string, data: Partial<Keyword>): Promise<ApiResponse> => {
  const keywords = await getKeywords(cookieId)
  // 检查是否已存在
  const exists = keywords.some(k => 
    k.keyword === data.keyword && 
    (k.item_id || '') === (data.item_id || '')
  )
  if (exists) {
    return { success: false, message: '该关键词已存在' }
  }
  keywords.push({
    keyword: data.keyword || '',
    reply: data.reply || '',
    item_id: data.item_id || '',
    priority: data.priority ?? 0,
    match_mode: data.match_mode || 'contains',
    conversation_stage: data.conversation_stage || '',
    enabled: data.enabled !== false,
    needs_human: data.needs_human === true,
    approval_status: data.approval_status || 'approved',
    type: 'text',
  } as Keyword)
  return saveKeywords(cookieId, keywords)
}

// 更新关键词
export const updateKeyword = async (
  cookieId: string, 
  oldKeyword: string, 
  oldItemId: string,
  data: Partial<Keyword>
): Promise<ApiResponse> => {
  const params = new URLSearchParams()
  if (oldItemId) {
    params.append('old_item_id', oldItemId)
  }
  const url = `${KEYWORD_PREFIX}/${cookieId}/${encodeURIComponent(oldKeyword)}${params.toString() ? '?' + params.toString() : ''}`
  return put(url, {
    account_id: data.account_id || cookieId,
    keyword: data.keyword || '',
    reply: data.reply || '',
    item_id: data.item_id || '',
    priority: data.priority ?? 0,
    match_mode: data.match_mode || 'contains',
    conversation_stage: data.conversation_stage || '',
    enabled: data.enabled !== false,
    needs_human: data.needs_human === true,
    approval_status: data.approval_status || 'approved',
  })
}

// 删除关键词（支持文本和图片类型）
export const deleteKeyword = async (
  cookieId: string, 
  keyword: string, 
  itemId: string,
  ruleId?: string
): Promise<ApiResponse> => {
  // 使用新的删除API，支持删除文本和图片类型的关键词
  const params = new URLSearchParams()
  if (itemId) {
    params.append('item_id', itemId)
  }
  if (ruleId) {
    params.append('rule_id', ruleId)
  }
  const url = `${KEYWORD_PREFIX}/${cookieId}/${encodeURIComponent(keyword)}${params.toString() ? '?' + params.toString() : ''}`
  
  return del<ApiResponse>(url)
}

// 批量添加关键词 - 通过 saveKeywords 实现
export const batchAddKeywords = async (cookieId: string, keywords: Partial<Keyword>[]): Promise<ApiResponse> => {
  const existing = await getKeywords(cookieId)
  const newKeywords = [...existing, ...keywords.map(k => ({
    keyword: k.keyword || '',
    reply: k.reply || '',
    item_id: k.item_id || '',
    priority: k.priority ?? 0,
    match_mode: k.match_mode || 'contains',
    conversation_stage: k.conversation_stage || '',
    enabled: k.enabled !== false,
    needs_human: k.needs_human === true,
    approval_status: k.approval_status || 'approved',
    type: 'text' as const,
  }))]
  return saveKeywords(cookieId, newKeywords)
}

// 批量删除关键词 - 通过 saveKeywords 实现
export const batchDeleteKeywords = async (cookieId: string, keywordIds: string[]): Promise<ApiResponse> => {
  const existing = await getKeywords(cookieId)
  const filtered = existing.filter(k => !keywordIds.includes(k.keyword))
  return saveKeywords(cookieId, filtered)
}

// 默认回复API前缀
const DEFAULT_REPLY_PREFIX = '/api/v1/cookies'

// 获取默认回复设置
export const getDefaultReply = async (cookieId: string): Promise<{ default_reply: string; reply_image: string; enabled: boolean; reply_once: boolean; reply_type: string; api_url: string; api_timeout: number }> => {
  const response = await get<ApiResponse<{ enabled: boolean; reply_content: string; reply_image: string; reply_once: boolean; reply_type?: string; api_url?: string; api_timeout?: number }>>(`${DEFAULT_REPLY_PREFIX}/${cookieId}/default-reply`)
  const result = response.data || { enabled: false, reply_content: '', reply_image: '', reply_once: false }
  return {
    default_reply: result.reply_content || '',
    reply_image: result.reply_image || '',
    enabled: result.enabled || false,
    reply_once: result.reply_once || false,
    reply_type: result.reply_type || 'text',
    api_url: result.api_url || '',
    api_timeout: result.api_timeout || 80,
  }
}

// 更新默认回复设置
export const updateDefaultReply = async (
  cookieId: string,
  defaultReply: string,
  enabled: boolean = true,
  replyOnce: boolean = false,
  replyImage: string = '',
  replyType: string = 'text',
  apiUrl: string = '',
  apiTimeout: number = 80
): Promise<ApiResponse> => {
  return put(`${DEFAULT_REPLY_PREFIX}/${cookieId}/default-reply`, {
    enabled,
    reply_content: defaultReply,
    reply_image: replyImage,
    reply_once: replyOnce,
    reply_type: replyType,
    api_url: apiUrl,
    api_timeout: apiTimeout,
  })
}

// 上传默认回复图片
export const uploadDefaultReplyImage = async (cookieId: string, image: File): Promise<{ success: boolean; image_url?: string; message?: string }> => {
  const formData = new FormData()
  formData.append('image', image)
  return post<ApiResponse<{ image_url?: string }>>(`${DEFAULT_REPLY_PREFIX}/${cookieId}/default-reply/upload-image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then((result) => ({
    success: result.success,
    image_url: result.data?.image_url,
    message: result.message,
  }))
}

// 导出关键词
export const exportKeywords = async (cookieId: string): Promise<Blob> => {
  const token = localStorage.getItem('auth_token')
  const response = await fetch(`${KEYWORD_PREFIX}/${cookieId}/export`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  if (!response.ok) throw new Error('导出失败')
  return response.blob()
}

// 导入关键词
export const importKeywords = async (
  cookieId: string,
  file: File
): Promise<ApiResponse<{ added: number; updated: number }>> => {
  const formData = new FormData()
  formData.append('file', file)
  return post<ApiResponse<{ added: number; updated: number }>>(`${KEYWORD_PREFIX}/${cookieId}/import`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

// 添加图片关键词
export const addImageKeyword = async (
  cookieId: string,
  keyword: string,
  image: File,
  itemId?: string
): Promise<ApiResponse<{ keyword: string; image_url: string; item_id?: string }>> => {
  const formData = new FormData()
  formData.append('keyword', keyword)
  formData.append('image', image)
  if (itemId) {
    formData.append('item_id', itemId)
  }
  return post(`${KEYWORD_PREFIX}/${cookieId}/image`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
