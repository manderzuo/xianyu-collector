import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { CheckCircle2, ChevronLeft, ChevronRight, Copy, Loader2, RefreshCw, Search, ShieldCheck, Users as UsersIcon } from 'lucide-react'
import {
  createAdminPlan,
  deleteUserFeature,
  getUserEntitlements,
  listAdminPlans,
  setUserFeature,
  setUserPlan,
  updatePlanFeature,
  type AdminPlan,
  type PlanFeatureConfig,
  type UserEntitlementSnapshot,
} from '@/api/entitlements'
import { getUsers } from '@/api/admin'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/request'
import type { User } from '@/types'

/** 平台已支持的功能授权清单：套餐矩阵与用户授权都以它为列/行基准。 */
const FEATURE_KEYS = [
  'account.manage',
  'card.auto_delivery',
  'product.auto_publish',
  'ai.smart_reply',
  'ai.builtin_reply',
  'keyword.reply',
] as const

const FEATURE_LABELS: Record<string, string> = {
  'account.manage': '闲鱼账号',
  'card.auto_delivery': '自动发货卡券',
  'product.auto_publish': '自动发布商品',
  'ai.smart_reply': 'AI 智能回复',
  'ai.builtin_reply': '内置AI自动回复',
  'keyword.reply': '关键词回复',
}

const FEATURE_HINTS: Record<string, string> = {
  'account.manage': '可管理的闲鱼账号数量',
  'card.auto_delivery': '自动发货卡券数量',
  'product.auto_publish': '自动发布商品数量',
  'ai.smart_reply': 'AI 智能回复开关',
  'ai.builtin_reply': '内置 AI 自动回复与预置关键词规则',
  'keyword.reply': '关键词规则数量',
}

const featureLabel = (key: string) => FEATURE_LABELS[key] || key

const DISABLED_FEATURE: PlanFeatureConfig = { enabled: false, limit: 0, unlimited: false }

const toDatetimeLocal = (value?: string | null): string => (value ? value.slice(0, 19) : '')

/** datetime-local 的值按本地时间解释，统一转成 UTC ISO 再发给后端。 */
const toIsoOrNull = (value: string): string | null => {
  const text = value.trim()
  if (!text) return null
  const parsed = new Date(text.length <= 16 ? `${text}:00` : text)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}

const formatExpiresAt = (value?: string | null): string => {
  if (!value) return '永不过期'
  return value.replace('T', ' ').slice(0, 19)
}

const isExpired = (value?: string | null): boolean => {
  if (!value) return false
  const time = new Date(value).getTime()
  return Number.isFinite(time) && time < Date.now()
}

type PlanDraft = Record<string, Record<string, PlanFeatureConfig>>

type OverrideDraft = { enabled: boolean; unlimited: boolean; limit: string }

const emptyOverrideDraft = (): OverrideDraft => ({ enabled: false, unlimited: false, limit: '0' })

const draftFromSnapshot = (overrides: UserEntitlementSnapshot['overrides']): Record<string, OverrideDraft> => {
  const result: Record<string, OverrideDraft> = {}
  for (const item of overrides || []) {
    result[item.feature_key] = {
      enabled: Boolean(item.enabled),
      unlimited: Boolean(item.unlimited),
      limit: item.limit === null || item.limit === undefined ? '' : String(item.limit),
    }
  }
  return result
}

export function Entitlements() {
  const { addToast } = useUIStore()
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const initialUserId = Number(searchParams.get('user_id') || 0) || null
  const initialTab = searchParams.get('tab') === 'plans' ? 'plans' : initialUserId ? 'users' : 'plans'
  const [tab, setTab] = useState<'plans' | 'users'>(initialTab)
  const [presetUserId, setPresetUserId] = useState<number | null>(initialUserId)

  const switchTab = (next: 'plans' | 'users') => {
    setTab(next)
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    if (next === 'plans') params.delete('user_id')
    setSearchParams(params, { replace: true })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="page-title">套餐权限</h1>
          <p className="page-description">
            VIP 开通、套餐变更与功能授权都在此页面独立维护，用户管理不再承担套餐编辑。
          </p>
        </div>
        <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-1 dark:border-slate-700 dark:bg-slate-800">
          <button
            type="button"
            className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${tab === 'plans' ? 'bg-white text-blue-600 shadow-sm dark:bg-slate-700 dark:text-blue-300' : 'text-slate-600 dark:text-slate-300'}`}
            onClick={() => switchTab('plans')}
          >
            套餐配置
          </button>
          <button
            type="button"
            className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${tab === 'users' ? 'bg-white text-blue-600 shadow-sm dark:bg-slate-700 dark:text-blue-300' : 'text-slate-600 dark:text-slate-300'}`}
            onClick={() => switchTab('users')}
          >
            用户授权
          </button>
        </div>
      </div>

      {tab === 'plans'
        ? <PlanCatalog addToast={addToast} onSwitchToUsers={() => switchTab('users')} />
        : (
          <UserAuthorization
            addToast={addToast}
            presetUserId={presetUserId}
            onPresetConsumed={() => setPresetUserId(null)}
            onOpenUserManagement={() => navigate('/admin/users')}
          />
        )}
    </div>
  )
}

interface ToastBridge {
  addToast: (options: { type: 'success' | 'error' | 'warning' | 'info'; message: string }) => void
}

function PlanCatalog({ addToast, onSwitchToUsers }: ToastBridge & { onSwitchToUsers: () => void }) {
  const [plans, setPlans] = useState<AdminPlan[]>([])
  const [draft, setDraft] = useState<PlanDraft>({})
  const [dirty, setDirty] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [newCode, setNewCode] = useState('')
  const [newName, setNewName] = useState('')
  const [copyFrom, setCopyFrom] = useState('')
  const [creating, setCreating] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listAdminPlans()
      const items = result.data?.items || []
      const nextDraft: PlanDraft = {}
      for (const plan of items) {
        nextDraft[plan.code] = {}
        for (const key of FEATURE_KEYS) {
          nextDraft[plan.code][key] = plan.features[key] ? { ...plan.features[key] } : { ...DISABLED_FEATURE }
        }
      }
      setPlans(items)
      setDraft(nextDraft)
      setDirty({})
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '套餐配置加载失败') })
    } finally {
      setLoading(false)
    }
  }, [addToast])

  useEffect(() => { void reload() }, [reload])

  const patchDraft = (planCode: string, featureKey: string, patch: Partial<PlanFeatureConfig>) => {
    setDraft((current) => ({
      ...current,
      [planCode]: {
        ...current[planCode],
        [featureKey]: { ...(current[planCode]?.[featureKey] || DISABLED_FEATURE), ...patch },
      },
    }))
    setDirty((current) => ({ ...current, [`${planCode}:${featureKey}`]: true }))
  }

  const saveFeature = async (planCode: string, featureKey: string) => {
    const feature = draft[planCode]?.[featureKey] || DISABLED_FEATURE
    if (!feature.unlimited) {
      const limit = Number(feature.limit)
      if (!Number.isInteger(limit) || limit < 0) {
        addToast({ type: 'warning', message: '配额必须是不小于 0 的整数，或勾选“无限”' })
        return
      }
    }
    const key = `${planCode}:${featureKey}`
    setSavingKey(key)
    try {
      await updatePlanFeature(planCode, featureKey, {
        enabled: feature.enabled,
        limit: feature.unlimited ? null : Number(feature.limit),
        unlimited: feature.unlimited,
      })
      addToast({ type: 'success', message: `${planCode} / ${featureLabel(featureKey)} 已保存` })
      setDirty((current) => ({ ...current, [key]: false }))
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '保存失败') })
    } finally {
      setSavingKey(null)
    }
  }

  const handleCreatePlan = async () => {
    const code = newCode.trim().toUpperCase()
    const name = newName.trim() || code
    if (!/^[A-Z0-9_\-]{2,32}$/.test(code)) {
      addToast({ type: 'warning', message: '套餐编码需为 2-32 位大写字母、数字、下划线或中划线' })
      return
    }
    setCreating(true)
    try {
      await createAdminPlan({ code, name, ...(copyFrom ? { copy_from: copyFrom } : {}) })
      addToast({ type: 'success', message: `套餐 ${name} 已创建` })
      setNewCode('')
      setNewName('')
      setCopyFrom('')
      await reload()
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '套餐创建失败') })
    } finally {
      setCreating(false)
    }
  }

  if (loading) {
    return (
      <div className="vben-card">
        <div className="vben-card-body flex items-center gap-2 text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" />
          正在加载套餐配置…
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-700 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-200">
        套餐配置决定用户开通该套餐后默认拥有的功能与配额。给单个用户开通 VIP 或临时调整功能，请切换到
        <button type="button" className="mx-1 font-medium underline" onClick={onSwitchToUsers}>用户授权</button>
        页签。单个账号的具体开通入口在用户列表中，不在用户编辑窗口。
      </div>

      {plans.length === 0 && (
        <div className="vben-card">
          <div className="vben-card-body text-sm text-slate-500">
            暂无套餐数据。系统初始化后应存在 NORMAL 与 VIP 两个默认套餐。
          </div>
        </div>
      )}

      {plans.map((plan) => (
        <div key={plan.code} className="vben-card">
          <div className="vben-card-header flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="vben-card-title">
              <ShieldCheck className="h-4 w-4" />
              {plan.name}（{plan.code}）
            </h2>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {plan.status === 'active' ? '启用中' : '已停用'}
              {plan.is_default ? ' · 默认套餐' : ''}
            </span>
          </div>
          <div className="vben-card-body overflow-x-auto">
            <table className="table-ios">
              <thead>
                <tr>
                  <th>功能</th>
                  <th className="w-24">启用</th>
                  <th className="w-32">配额</th>
                  <th className="w-24">无限</th>
                  <th className="w-28">操作</th>
                </tr>
              </thead>
              <tbody>
                {FEATURE_KEYS.map((featureKey) => {
                  const feature = draft[plan.code]?.[featureKey] || DISABLED_FEATURE
                  const key = `${plan.code}:${featureKey}`
                  const isDirty = Boolean(dirty[key])
                  return (
                    <tr key={featureKey}>
                      <td>
                        <div className="font-medium text-slate-700 dark:text-slate-200">{featureLabel(featureKey)}</div>
                        <div className="text-xs text-slate-400">{FEATURE_HINTS[featureKey] || featureKey}</div>
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={feature.enabled}
                          onChange={(event) => patchDraft(plan.code, featureKey, { enabled: event.target.checked })}
                        />
                      </td>
                      <td>
                        <input
                          className="input-ios w-24 py-1"
                          type="number"
                          min={0}
                          step={1}
                          disabled={feature.unlimited}
                          value={feature.unlimited ? '' : (feature.limit ?? '')}
                          placeholder={feature.unlimited ? '无限' : '0'}
                          onChange={(event) => patchDraft(plan.code, featureKey, {
                            limit: event.target.value === '' ? null : Number(event.target.value),
                          })}
                        />
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={feature.unlimited}
                          onChange={(event) => patchDraft(plan.code, featureKey, { unlimited: event.target.checked })}
                        />
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn-ios-primary px-3 py-1 text-xs"
                          disabled={!isDirty || savingKey === key}
                          onClick={() => void saveFeature(plan.code, featureKey)}
                        >
                          {savingKey === key ? '保存中' : isDirty ? '保存' : '已保存'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <div className="vben-card">
        <div className="vben-card-header">
          <h2 className="vben-card-title">
            <Copy className="h-4 w-4" />
            新增套餐
          </h2>
        </div>
        <div className="vben-card-body grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="input-group">
            <label className="input-label">套餐编码 <span className="text-red-500">*</span></label>
            <input
              className="input-ios"
              value={newCode}
              maxLength={32}
              placeholder="例如 SVIP"
              onChange={(event) => setNewCode(event.target.value)}
            />
          </div>
          <div className="input-group">
            <label className="input-label">套餐名称</label>
            <input
              className="input-ios"
              value={newName}
              maxLength={64}
              placeholder="留空则与编码相同"
              onChange={(event) => setNewName(event.target.value)}
            />
          </div>
          <div className="input-group">
            <label className="input-label">复制功能授权</label>
            <select className="input-ios" value={copyFrom} onChange={(event) => setCopyFrom(event.target.value)}>
              <option value="">不复制（全部关闭，需手动配置）</option>
              {plans.map((plan) => (
                <option key={plan.code} value={plan.code}>从 {plan.name}（{plan.code}）复制</option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-3">
            <button type="button" className="btn-ios-primary" disabled={creating} onClick={() => void handleCreatePlan()}>
              {creating && <Loader2 className="h-4 w-4 animate-spin" />}
              新增套餐
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

interface UserAuthorizationProps extends ToastBridge {
  presetUserId: number | null
  onPresetConsumed: () => void
  onOpenUserManagement: () => void
}

function UserAuthorization({ addToast, presetUserId, onPresetConsumed, onOpenUserManagement }: UserAuthorizationProps) {
  const [plans, setPlans] = useState<AdminPlan[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [loadingUsers, setLoadingUsers] = useState(false)

  const [selectedUser, setSelectedUser] = useState<User | null>(null)
  const [snapshot, setSnapshot] = useState<UserEntitlementSnapshot | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  const [planCode, setPlanCode] = useState('NORMAL')
  const [planExpiresAt, setPlanExpiresAt] = useState('')
  const [featureDraft, setFeatureDraft] = useState<Record<string, OverrideDraft>>({})
  // 待删除覆盖的功能键：删除后该功能回落到套餐默认授权。
  const [pendingDeletes, setPendingDeletes] = useState<string[]>([])
  const [savingPlan, setSavingPlan] = useState(false)
  const [savingFeatures, setSavingFeatures] = useState(false)

  const planOptions = useMemo(() => plans.filter((plan) => plan.status === 'active'), [plans])

  useEffect(() => {
    void (async () => {
      try {
        const result = await listAdminPlans()
        setPlans(result.data?.items || [])
      } catch (error) {
        addToast({ type: 'error', message: getApiErrorMessage(error, '套餐列表加载失败') })
      }
    })()
  }, [addToast])

  const loadUsers = useCallback(async () => {
    setLoadingUsers(true)
    try {
      const result = await getUsers({ page, pageSize, username: appliedSearch })
      if (!result.success) {
        setUsers([])
        setTotal(0)
        addToast({ type: 'error', message: result.message || '用户列表加载失败' })
        return
      }
      setUsers(result.data || [])
      setTotal(result.total || 0)
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '用户列表加载失败') })
    } finally {
      setLoadingUsers(false)
    }
  }, [addToast, appliedSearch, page, pageSize])

  useEffect(() => { void loadUsers() }, [loadUsers])

  const loadDetail = useCallback(async (target: User) => {
    setSelectedUser(target)
    setLoadingDetail(true)
    setSnapshot(null)
    try {
      const result = await getUserEntitlements(target.user_id)
      const data = result.data
      if (!data) {
        addToast({ type: 'error', message: result.message || '权限详情加载失败' })
        return
      }
      const features = draftFromSnapshot(data.overrides)
      const nextPlan = data.plan_code || target.plan_code || 'NORMAL'
      const nextExpires = toDatetimeLocal(data.plan_expires_at)
      setSnapshot(data)
      setPlanCode(nextPlan)
      setPlanExpiresAt(nextExpires)
      setFeatureDraft(features)
      setPendingDeletes([])
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '权限详情加载失败') })
    } finally {
      setLoadingDetail(false)
    }
  }, [addToast])

  // 从用户管理跳转过来时，自动定位到该用户
  useEffect(() => {
    if (!presetUserId) return
    const target = users.find((user) => user.user_id === presetUserId)
    if (target) {
      onPresetConsumed()
      void loadDetail(target)
      return
    }
    if (!loadingUsers) {
      void (async () => {
        try {
          const result = await getUsers({ page: 1, pageSize: 100 })
          const found = (result.data || []).find((user) => user.user_id === presetUserId)
          onPresetConsumed()
          if (found) void loadDetail(found)
          else addToast({ type: 'warning', message: '未找到该用户，请手动搜索用户名' })
        } catch (error) {
          onPresetConsumed()
          addToast({ type: 'error', message: getApiErrorMessage(error, '用户定位失败') })
        }
      })()
    }
  }, [presetUserId, users, loadingUsers, loadDetail, onPresetConsumed, addToast])

  const patchFeature = (featureKey: string, patch: Partial<OverrideDraft>) => {
    // 手动编辑即取消“跟随套餐”的删除意图
    setPendingDeletes((current) => current.filter((key) => key !== featureKey))
    setFeatureDraft((current) => ({ ...current, [featureKey]: { ...(current[featureKey] || emptyOverrideDraft()), ...patch } }))
  }

  /** 标记某功能回落到套餐默认：保存时删除该用户的功能覆盖记录。 */
  const clearFeatureOverride = (featureKey: string) => {
    setPendingDeletes((current) => (current.includes(featureKey) ? current : [...current, featureKey]))
  }

  /** 取消“跟随套餐”标记，恢复为当前已保存的覆盖值。 */
  const cancelClearOverride = (featureKey: string) => {
    setPendingDeletes((current) => current.filter((key) => key !== featureKey))
  }

  const originalDraft = useMemo(
    () => (snapshot ? draftFromSnapshot(snapshot.overrides) : {}),
    [snapshot],
  )

  const dirtyFeatureKeys = useMemo(() => {
    if (!selectedUser) return [] as string[]
    return FEATURE_KEYS.filter((key) => {
      if (pendingDeletes.includes(key)) return true
      const before = originalDraft[key]
      const after = featureDraft[key]
      if (!before && !after) return false
      if (!before || !after) return true
      return before.enabled !== after.enabled || before.unlimited !== after.unlimited || before.limit !== after.limit
    })
  }, [selectedUser, featureDraft, originalDraft, pendingDeletes])

  const savePlan = async () => {
    if (!selectedUser) return
    setSavingPlan(true)
    try {
      await setUserPlan(selectedUser.user_id, { plan_code: planCode, plan_expires_at: toIsoOrNull(planExpiresAt) })
      addToast({ type: 'success', message: `已更新 ${selectedUser.username} 的套餐` })
      await loadDetail(selectedUser)
      await loadUsers()
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '套餐更新失败') })
    } finally {
      setSavingPlan(false)
    }
  }

  const saveFeatures = async () => {
    if (!selectedUser) return
    if (dirtyFeatureKeys.length === 0) {
      addToast({ type: 'info', message: '功能授权没有变化' })
      return
    }
    for (const key of dirtyFeatureKeys) {
      if (pendingDeletes.includes(key)) continue
      const feature = featureDraft[key] || emptyOverrideDraft()
      if (!feature.unlimited) {
        const limit = Number(feature.limit)
        if (!Number.isInteger(limit) || limit < 0) {
          addToast({ type: 'warning', message: `${featureLabel(key)} 的配额必须是不小于 0 的整数，或勾选“无限”` })
          return
        }
      }
    }
    setSavingFeatures(true)
    try {
      for (const key of dirtyFeatureKeys) {
        if (pendingDeletes.includes(key)) {
          // 标为“跟随套餐”：删除覆盖记录，让该功能回落到套餐默认授权
          await deleteUserFeature(selectedUser.user_id, key)
          continue
        }
        const feature = featureDraft[key] || emptyOverrideDraft()
        await setUserFeature(selectedUser.user_id, key, {
          enabled: feature.enabled,
          limit: feature.unlimited ? null : Number(feature.limit),
          unlimited: feature.unlimited,
        })
      }
      addToast({ type: 'success', message: `已更新 ${selectedUser.username} 的功能授权` })
      await loadDetail(selectedUser)
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '功能授权保存失败') })
    } finally {
      setSavingFeatures(false)
    }
  }

  const rangeLabel = useMemo(() => {
    if (total === 0) return '共 0 条'
    const start = (page - 1) * pageSize + 1
    return `显示 ${start}-${Math.min(page * pageSize, total)} 条，共 ${total} 条`
  }, [page, pageSize, total])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-200">
        <div className="font-medium">用户开通与功能授权</div>
        <div className="mt-1">
          在此为用户开通 VIP、变更套餐到期时间，或单独开启/关闭某项功能与配额。
          账号资料（用户名、角色、状态、密码）仍由
          <button type="button" className="mx-1 font-medium underline" onClick={onOpenUserManagement}>用户管理</button>
          维护。
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="vben-card flex flex-col">
          <div className="vben-card-header flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="vben-card-title">
              <UsersIcon className="h-4 w-4" />
              选择用户
            </h2>
            <div className="flex items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <input
                  className="w-40 rounded-md border border-slate-300 bg-white py-1.5 pl-8 pr-2 text-sm text-slate-700 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/40 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200 sm:w-48"
                  value={search}
                  placeholder="搜索用户名"
                  onChange={(event) => setSearch(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter') return
                    setAppliedSearch(search.trim())
                    setPage(1)
                  }}
                />
              </div>
              <button
                type="button"
                className="btn-ios-secondary"
                onClick={() => {
                  setAppliedSearch(search.trim())
                  setPage(1)
                }}
              >
                搜索
              </button>
              <button type="button" className="btn-ios-secondary" onClick={() => void loadUsers()} disabled={loadingUsers}>
                {loadingUsers ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              </button>
            </div>
          </div>
          <div className="vben-card-body max-h-[520px] overflow-auto p-0">
            <table className="table-ios">
              <thead className="sticky top-0 z-10 bg-white dark:bg-slate-800">
                <tr>
                  <th>用户</th>
                  <th>套餐</th>
                  <th>到期日</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {users.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-8 text-center text-slate-500 dark:text-slate-400">
                      {loadingUsers ? '正在加载用户…' : '暂无用户数据'}
                    </td>
                  </tr>
                ) : users.map((user) => (
                  <tr key={user.user_id} className={selectedUser?.user_id === user.user_id ? 'bg-blue-50/60 dark:bg-blue-900/20' : undefined}>
                    <td>
                      <div className="font-medium text-slate-700 dark:text-slate-200">{user.username}</div>
                      <div className="text-xs text-slate-400">
                        #{user.user_id}
                        {user.is_admin ? ' · 管理员' : ''}
                        {user.cloud_mode ? ' · 云端账号' : ''}
                      </div>
                    </td>
                    <td>
                      <span className="badge-primary">{user.plan_code || 'NORMAL'}</span>
                    </td>
                    <td className={`whitespace-nowrap text-xs ${isExpired(user.plan_expires_at) ? 'text-red-500' : 'text-slate-500 dark:text-slate-400'}`}>
                      {formatExpiresAt(user.plan_expires_at)}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="rounded-lg px-2.5 py-1.5 text-sm text-blue-600 transition-colors hover:bg-blue-50 dark:text-blue-400 dark:hover:bg-blue-900/20"
                        onClick={() => void loadDetail(user)}
                      >
                        配置权限
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="vben-card-footer flex items-center justify-between gap-3 px-4 py-3 text-sm text-slate-500 dark:text-slate-400">
            <span>{rangeLabel}</span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded border border-slate-300 p-1.5 disabled:opacity-50 dark:border-slate-600"
                disabled={page <= 1}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <span>第 {page} / {totalPages} 页</span>
              <button
                type="button"
                className="rounded border border-slate-300 p-1.5 disabled:opacity-50 dark:border-slate-600"
                disabled={page >= totalPages}
                onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
              >
                <ChevronRight className="h-4 w-4" />
              </button>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1 text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300"
                value={pageSize}
                onChange={(event) => {
                  setPageSize(Number(event.target.value))
                  setPage(1)
                }}
              >
                <option value={10}>10 条/页</option>
                <option value={20}>20 条/页</option>
                <option value={50}>50 条/页</option>
              </select>
            </div>
          </div>
        </div>

        <div className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title">
              <ShieldCheck className="h-4 w-4" />
              权限配置
              {selectedUser && <span className="ml-2 text-sm font-normal text-slate-500">{selectedUser.username}</span>}
            </h2>
          </div>
          <div className="vben-card-body">
            {!selectedUser && (
              <p className="text-sm text-slate-500 dark:text-slate-400">请在左侧选择用户后进行 VIP 开通与功能授权。</p>
            )}
            {selectedUser && loadingDetail && (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                正在加载权限详情…
              </div>
            )}
            {selectedUser && !loadingDetail && (
              <div className="space-y-6">
                {selectedUser.is_admin && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200">
                    管理员始终拥有全部功能且不受配额限制，套餐设置对该账号不生效。
                  </div>
                )}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">套餐与到期时间</h3>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div className="input-group">
                      <label className="input-label">套餐</label>
                      <select className="input-ios" value={planCode} onChange={(event) => setPlanCode(event.target.value)}>
                        {planOptions.length === 0 && <option value={planCode}>{planCode}</option>}
                        {planOptions.map((plan) => (
                          <option key={plan.code} value={plan.code}>{plan.name}（{plan.code}）</option>
                        ))}
                      </select>
                    </div>
                    <div className="input-group">
                      <label className="input-label">套餐到期时间</label>
                      <input
                        className="input-ios"
                        type="datetime-local"
                        step={1}
                        value={planExpiresAt}
                        onChange={(event) => setPlanExpiresAt(event.target.value)}
                      />
                      <p className="mt-1 text-xs text-slate-400">留空表示永不过期；精确到秒</p>
                    </div>
                  </div>
                  <button type="button" className="btn-ios-primary" disabled={savingPlan} onClick={() => void savePlan()}>
                    {savingPlan && <Loader2 className="h-4 w-4 animate-spin" />}
                    保存套餐
                  </button>
                </section>

                <section className="space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">功能授权</h3>
                    <span className="text-xs text-slate-400">
                      勾选“启用”并按需设置配额即为单独授权（覆盖套餐默认值）；点“跟随套餐”可删除该用户的单独授权
                    </span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="table-ios">
                      <thead>
                        <tr>
                          <th>功能</th>
                          <th className="w-20">启用</th>
                          <th className="w-28">配额</th>
                          <th className="w-20">无限</th>
                          <th className="w-28">覆盖状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {FEATURE_KEYS.map((featureKey) => {
                          const feature = featureDraft[featureKey] || emptyOverrideDraft()
                          const hasOverride = Boolean(originalDraft[featureKey])
                          const willClear = pendingDeletes.includes(featureKey)
                          return (
                            <tr key={featureKey} className={willClear ? 'opacity-60' : undefined}>
                              <td>
                                <div className="text-slate-700 dark:text-slate-200">{featureLabel(featureKey)}</div>
                                <div className="text-xs text-slate-400">{FEATURE_HINTS[featureKey] || featureKey}</div>
                              </td>
                              <td>
                                <input
                                  type="checkbox"
                                  checked={feature.enabled}
                                  disabled={willClear}
                                  onChange={(event) => patchFeature(featureKey, { enabled: event.target.checked })}
                                />
                              </td>
                              <td>
                                <input
                                  className="input-ios w-24 py-1"
                                  type="number"
                                  min={0}
                                  step={1}
                                  disabled={feature.unlimited || willClear}
                                  value={feature.unlimited ? '' : feature.limit}
                                  placeholder={feature.unlimited ? '无限' : '0'}
                                  onChange={(event) => patchFeature(featureKey, { limit: event.target.value })}
                                />
                              </td>
                              <td>
                                <input
                                  type="checkbox"
                                  checked={feature.unlimited}
                                  disabled={willClear}
                                  onChange={(event) => patchFeature(featureKey, { unlimited: event.target.checked })}
                                />
                              </td>
                              <td>
                                {willClear ? (
                                  <div className="space-y-1">
                                    <span className="block text-xs text-amber-600 dark:text-amber-400">将跟随套餐</span>
                                    <button
                                      type="button"
                                      className="text-xs text-blue-600 underline dark:text-blue-400"
                                      onClick={() => cancelClearOverride(featureKey)}
                                    >
                                      撤销
                                    </button>
                                  </div>
                                ) : hasOverride ? (
                                  <div className="space-y-1">
                                    <span className="block text-xs text-indigo-600 dark:text-indigo-400">已单独授权</span>
                                    <button
                                      type="button"
                                      className="text-xs text-slate-500 underline dark:text-slate-400"
                                      title="删除该用户的单独授权，改回套餐默认值"
                                      onClick={() => clearFeatureOverride(featureKey)}
                                    >
                                      跟随套餐
                                    </button>
                                  </div>
                                ) : (
                                  <span className="text-xs text-slate-400">跟随套餐</span>
                                )}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                  <div className="flex items-center gap-3">
                    <button type="button" className="btn-ios-primary" disabled={savingFeatures} onClick={() => void saveFeatures()}>
                      {savingFeatures && <Loader2 className="h-4 w-4 animate-spin" />}
                      保存功能授权
                    </button>
                    {dirtyFeatureKeys.length > 0 && (
                      <span className="text-xs text-amber-600 dark:text-amber-400">
                        {dirtyFeatureKeys.length} 项待保存
                      </span>
                    )}
                    {dirtyFeatureKeys.length === 0 && snapshot && (
                      <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        与已保存结果一致
                      </span>
                    )}
                  </div>
                </section>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default Entitlements
