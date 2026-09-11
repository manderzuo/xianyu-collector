import { del, get, post, put } from '@/utils/request'
import type { ApiResponse, EntitlementQuota } from '@/types'

export interface PlanFeatureConfig {
  enabled: boolean
  limit: number | null
  unlimited: boolean
  config?: Record<string, unknown>
}

export interface AdminPlan {
  id: number
  code: string
  name: string
  status: string
  is_default: boolean
  features: Record<string, PlanFeatureConfig>
}

export const listAdminPlans = (): Promise<ApiResponse<{ items: AdminPlan[]; total: number }>> =>
  get('/api/v1/admin/entitlements/plans')

export const updatePlanFeature = (
  planCode: string,
  featureKey: string,
  payload: { enabled?: boolean; limit?: number | null; unlimited?: boolean }
): Promise<ApiResponse> =>
  put(`/api/v1/admin/entitlements/plans/${encodeURIComponent(planCode)}/features/${encodeURIComponent(featureKey)}`, payload)

export const createAdminPlan = (
  payload: { code: string; name: string; copy_from?: string }
): Promise<ApiResponse<AdminPlan>> =>
  post('/api/v1/admin/entitlements/plans', payload)

export interface UserEntitlementSnapshot {
  user_id: number
  plan_code: string
  plan_expires_at?: string | null
  overrides: Array<{ feature_key: string; enabled?: boolean | null; limit?: number | null; unlimited?: boolean | null; expires_at?: string | null; reason?: string | null }>
  effective: { quotas?: Record<string, EntitlementQuota>; features?: Record<string, boolean> }
}

/** 读取某个用户的套餐与单用户功能授权（本地模式或云端统一认证）。 */
export const getUserEntitlements = (userId: number): Promise<ApiResponse<UserEntitlementSnapshot>> =>
  get(`/api/v1/admin/entitlements/users/${userId}`)

/** 给用户开通/变更套餐；plan_expires_at 显式传 null 表示清空到期日。 */
export const setUserPlan = (
  userId: number,
  payload: { plan_code: string; plan_expires_at?: string | null }
): Promise<ApiResponse<{ user_id: number; plan_code?: string; plan_expires_at?: string | null }>> =>
  put(`/api/v1/admin/entitlements/users/${userId}/plan`, payload)

/** 新增或覆盖某个用户的功能授权。 */
export const setUserFeature = (
  userId: number,
  featureKey: string,
  payload: { enabled?: boolean; limit?: number | null; unlimited?: boolean; reason?: string }
): Promise<ApiResponse> =>
  put(`/api/v1/admin/entitlements/users/${userId}/features/${encodeURIComponent(featureKey)}`, payload)

/** 删除某个用户的功能授权覆盖，回落到套餐默认值。 */
export const deleteUserFeature = (userId: number, featureKey: string): Promise<ApiResponse> =>
  del(`/api/v1/admin/entitlements/users/${userId}/features/${encodeURIComponent(featureKey)}`)
