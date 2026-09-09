import { post, get } from '@/utils/request'
import { normalizeAuthFooterAdSettings, normalizeLoginBrandingSettings } from '@/api/settings'
import type { AuthFooterAdSettings, LoginBrandingSettings, LoginRequest, LoginResponse, ApiResponse } from '@/types'

const AUTH_PREFIX = '/api/v1/auth'
const SYSTEM_PREFIX = '/api/v1/system-settings'
const CAPTCHA_PREFIX = '/api/v1/captcha'
const GEETEST_PREFIX = '/api/v1/geetest'
const CLOUD_AUTH_OPERATION_TIMEOUT = 180000

// 缓存公共设置，避免重复请求
let publicSettingsCache: Record<string, unknown> | null = null
let publicSettingsPromise: Promise<Record<string, unknown>> | null = null

/**
 * 获取公共系统设置（带缓存）
 * 同一页面生命周期内只请求一次
 */
const getPublicSettings = async (forceRefresh = false): Promise<Record<string, unknown>> => {
  // 如果已有缓存，直接返回
  if (!forceRefresh && publicSettingsCache) {
    return publicSettingsCache
  }
  // 如果正在请求中，等待该请求完成
  if (publicSettingsPromise) {
    return publicSettingsPromise
  }
  // 发起新请求
  publicSettingsPromise = get<ApiResponse<Record<string, unknown>>>(`${SYSTEM_PREFIX}/public`)
    .then((response) => {
      const settings = response.data || {}
      publicSettingsCache = settings
      return settings
    })
    .finally(() => {
      publicSettingsPromise = null
    })
  return publicSettingsPromise
}

// 用户登录
export const login = async (data: LoginRequest): Promise<LoginResponse> => {
  const result = await post<ApiResponse<{
    access_token?: string
    token?: string
    refresh_token?: string
  user?: { id?: number; username?: string; nickname?: string; role?: string; plan_code?: string; account_limit?: number | null; entitlements?: import('@/types').UserEntitlements }
    user_id?: number
    username?: string
    is_admin?: boolean
    account_limit?: number | null
    role?: string
    plan_code?: string
    entitlements?: import('@/types').UserEntitlements
  }>>(`${AUTH_PREFIX}/login`, data, { timeout: CLOUD_AUTH_OPERATION_TIMEOUT })
  const payload = result.data || {}
  const token = payload.token || payload.access_token
  const user = payload.user
  return {
    success: result.success,
    message: result.message,
    token,
    refresh_token: payload.refresh_token || token,
    user_id: payload.user_id ?? user?.id,
    username: payload.username || user?.username,
    is_admin: payload.is_admin ?? user?.role === 'admin',
    account_limit: payload.account_limit ?? user?.account_limit,
    role: (payload.role || user?.role)?.toUpperCase() as LoginResponse['role'],
    plan_code: payload.plan_code || user?.plan_code,
    entitlements: payload.entitlements || user?.entitlements,
  }
}

// 验证 Token
export const verifyToken = async (): Promise<{ authenticated: boolean; user_id?: number; username?: string; is_admin?: boolean; account_limit?: number | null; role?: string; plan_code?: string; auth_version?: number; entitlements?: import('@/types').UserEntitlements }> => {
  const result = await get<ApiResponse<{
    valid?: boolean
    authenticated?: boolean
    user_id?: number
    username?: string
    is_admin?: boolean
    account_limit?: number | null
    role?: string
    plan_code?: string
    auth_version?: number
    entitlements?: import('@/types').UserEntitlements
  }>>(`${AUTH_PREFIX}/verify`)
  const payload = result.data || {}
  return {
    authenticated: Boolean(payload.authenticated ?? payload.valid ?? result.success),
    user_id: payload.user_id,
    username: payload.username,
    is_admin: payload.is_admin,
    account_limit: payload.account_limit,
    role: payload.role,
    plan_code: payload.plan_code,
    auth_version: payload.auth_version,
    entitlements: payload.entitlements,
  }
}

// 用户登出
export const logout = (): Promise<ApiResponse> => {
  return post(`${AUTH_PREFIX}/logout`)
}

// 获取注册状态 - 从系统设置获取
export const getRegistrationStatus = async (): Promise<{ enabled: boolean }> => {
  // 注册开关可能由管理员在另一个页面或另一个浏览器标签页修改，
  // 不能复用登录页生命周期内的旧公开设置缓存。
  const settings = await getPublicSettings(true)
  // 处理多种可能的值类型：true, 'true', 1, '1'
  const value = settings.registration_enabled
  if (value === undefined || value === null || value === '') return { enabled: true }
  return { enabled: value === true || value === 'true' || value === 1 || value === '1' }
}

// 获取登录验证码开关状态
export const getLoginCaptchaStatus = async (): Promise<{ enabled: boolean }> => {
  const settings = await getPublicSettings()
  const value = settings.login_captcha_enabled
  // 未配置验证码服务时默认关闭，避免登录页被不可用的滑块阻断。
  if (value === undefined || value === null) {
    return { enabled: false }
  }
  return { enabled: value === true || value === 'true' || value === 1 || value === '1' }
}

// 获取登录页品牌配置
export const getLoginBrandingSettings = async (): Promise<LoginBrandingSettings> => {
  const settings = await getPublicSettings()
  return normalizeLoginBrandingSettings(settings)
}

export const getAuthFooterAdSettings = async (): Promise<AuthFooterAdSettings> => {
  const settings = await getPublicSettings()
  return normalizeAuthFooterAdSettings(settings)
}

// 生成图形验证码
export const generateCaptcha = async (sessionId: string): Promise<{ success: boolean; captcha_image?: string }> => {
  const result = await post<{ success: boolean; data: { captcha_image: string; session_id: string }; message: string }>(
    `${CAPTCHA_PREFIX}/generate`,
    { session_id: sessionId }
  )
  return { success: result.success, captcha_image: result.data?.captcha_image }
}

// 验证图形验证码
export const verifyCaptcha = async (sessionId: string, captchaCode: string): Promise<{ success: boolean }> => {
  const result = await post<{ success: boolean; message: string }>(
    `${CAPTCHA_PREFIX}/verify`,
    { session_id: sessionId, captcha_code: captchaCode }
  )
  return { success: result.success }
}

// 发送邮箱验证码
export const sendVerificationCode = async (email: string, type: string, sessionId: string): Promise<ApiResponse> => {
  return post(`${CAPTCHA_PREFIX}/send-email-code`, { email, type, session_id: sessionId })
}

// 用户注册 - 使用新后端接口
export const register = (data: { 
  username: string
  invite_code: string
  password: string
  session_id?: string
}): Promise<ApiResponse> => {
  return post(`${AUTH_PREFIX}/register`, {
    username: data.username,
    invite_code: data.invite_code,
    password: data.password,
    ...(data.session_id ? { session_id: data.session_id } : {}),
  }, { timeout: CLOUD_AUTH_OPERATION_TIMEOUT })
}

// ==================== 极验滑动验证码 ====================

// 极验验证码初始化响应类型
interface GeetestRegisterResponse {
  success: boolean
  code: number
  message: string
  data?: {
    success: number
    gt: string
    challenge: string
    new_captcha: boolean
  }
}

// 极验二次验证响应类型
interface GeetestValidateResponse {
  success: boolean
  code: number
  message: string
}

// 获取极验验证码初始化参数
export const getGeetestRegister = (): Promise<GeetestRegisterResponse> => {
  return get(`${GEETEST_PREFIX}/register`)
}

// 极验二次验证
export const geetestValidate = (data: {
  challenge: string
  validate: string
  seccode: string
}): Promise<GeetestValidateResponse> => {
  return post(`${GEETEST_PREFIX}/validate`, data)
}

// 检查管理员密码是否为默认值
export const checkAdminDefaultPassword = (): Promise<ApiResponse<{ is_default: boolean }>> => {
  return get(`${AUTH_PREFIX}/check-default-password`)
}


// 发送重置密码验证码
export const sendResetPasswordCode = async (email: string): Promise<ApiResponse> => {
  const sessionId = `reset_${Math.random().toString(36).substr(2, 9)}_${Date.now()}`
  return post(`${CAPTCHA_PREFIX}/send-email-code`, { email, type: 'reset_password', session_id: sessionId })
}

// 重置密码
export const resetPassword = (data: { email: string; verification_code: string; new_password: string }): Promise<ApiResponse> => {
  return post(`${AUTH_PREFIX}/reset-password`, data)
}
