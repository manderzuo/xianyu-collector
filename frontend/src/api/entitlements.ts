import { get, put } from '@/utils/request'
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

export interface UserEntitlementSnapshot {
  user_id: number
  plan_code: string
  plan_expires_at?: string | null
  overrides: Array<{ feature_key: string; enabled?: boolean | null; limit?: number | null; unlimited?: boolean | null }>
  effective: { quotas?: Record<string, EntitlementQuota>; features?: Record<string, boolean> }
}
