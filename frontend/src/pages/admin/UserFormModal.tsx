import { useMemo, useState } from 'react'
import { Cloud, Loader2, ShieldCheck, X } from 'lucide-react'
import { addUser, updateUser, type AdminUserApiItem, type CreateAdminUserPayload, type UpdateAdminUserPayload } from '@/api/admin'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/request'
import type { User, UserRole, UserStatus } from '@/types'

interface Props {
  initial: User | null
  onClose: () => void
  onSaved: (user: User, mode: 'create' | 'update') => void
  /** 跳转到“套餐权限”页面处理该用户的 VIP/功能授权。 */
  onOpenEntitlements?: (user: User) => void
}

interface UserFormState {
  username: string
  email: string
  phone: string
  password: string
  confirmPassword: string
  role: UserRole
  status: UserStatus
  account_limit: string
}

// 后端到期日为北京时间 naive 字符串（如 '2026-06-25T14:30:00'），
// 这里只做只读展示，套餐与到期时间统一由“套餐权限”页面维护。
const formatPlanExpiresAt = (value?: string | null): string => {
  if (!value) return '永不过期'
  return value.replace('T', ' ').slice(0, 19)
}

const createInitialState = (initial: User | null): UserFormState => ({
  username: initial?.username ?? '',
  email: initial?.email ?? '',
  phone: initial?.phone ?? '',
  password: '',
  confirmPassword: '',
  role: initial?.role ?? (initial?.is_admin ? 'ADMIN' : 'MEMBER'),
  status: initial?.status ?? 'ACTIVE',
  account_limit: initial?.account_limit != null ? String(initial.account_limit) : '',
})

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: 'ADMIN', label: '管理员' },
  { value: 'OPERATOR', label: '运营人员' },
  { value: 'MEMBER', label: '普通用户' },
]

const toUser = (item: AdminUserApiItem): User => ({
  user_id: item.id,
  username: item.username,
  email: item.email,
  phone: item.phone,
  role: item.role,
  status: item.status,
  is_admin: item.is_admin,
  cloud_mode: item.cloud_mode,
  account_limit: item.account_limit,
  plan_code: item.plan_code,
  plan_expires_at: item.plan_expires_at,
  expire_at: item.expire_at,
})

export function UserFormModal({ initial, onClose, onSaved, onOpenEntitlements }: Props) {
  const { addToast } = useUIStore()
  const [form, setForm] = useState<UserFormState>(() => createInitialState(initial))
  const [saving, setSaving] = useState(false)

  const isEditMode = !!initial
  // 云端账号（统一认证服务）的资料与套餐都不在本机修改，本窗口仅做只读展示。
  const isCloudEdit = isEditMode && Boolean(initial?.cloud_mode)
  const statusOptions = useMemo<Array<{ value: UserStatus; label: string }>>(() => {
    const options: Array<{ value: UserStatus; label: string }> = [
      { value: 'ACTIVE', label: '正常' },
      { value: 'PENDING', label: '待审批' },
      { value: 'INACTIVE', label: '停用' },
      { value: 'SUSPENDED', label: '封禁' },
    ]
    if (initial?.status === 'DELETED') {
      options.push({ value: 'DELETED', label: '已删除' })
    }
    return options
  }, [initial?.status])

  const updateField = <K extends keyof UserFormState>(field: K, value: UserFormState[K]) => {
    setForm((current) => ({ ...current, [field]: value }))
  }

  const handleSave = async () => {
    const username = form.username.trim()
    const email = form.email.trim()
    const phone = form.phone.trim()
    const password = form.password.trim()
    const confirmPassword = form.confirmPassword.trim()
    const accountLimitText = form.account_limit.trim()
    const accountLimit = accountLimitText === '' ? null : Number(accountLimitText)

    if (!username) {
      addToast({ type: 'warning', message: '请输入用户名' })
      return
    }

    if (email && !/^\S+@\S+\.\S+$/.test(email)) {
      addToast({ type: 'warning', message: '请输入正确的邮箱地址' })
      return
    }

    if (!isEditMode && !password) {
      addToast({ type: 'warning', message: '请输入登录密码' })
      return
    }

    if (password && password.length < 6) {
      addToast({ type: 'warning', message: '密码长度不能少于6位' })
      return
    }

    if (password !== confirmPassword) {
      addToast({ type: 'warning', message: '两次输入的密码不一致' })
      return
    }

    if (accountLimitText && (accountLimit === null || !Number.isInteger(accountLimit) || accountLimit <= 0)) {
      addToast({ type: 'warning', message: '可添加账号数量必须为正整数' })
      return
    }

    setSaving(true)
    try {
      // 套餐与到期时间不在此提交：VIP 开通和功能授权由“套餐权限”页面负责。
      const basePayload = {
        username,
        email,
        phone,
        role: form.role,
        status: form.status,
        account_limit: accountLimit,
      }

      let result
      if (isEditMode && initial) {
        const payload: UpdateAdminUserPayload = { ...basePayload, password: password || undefined }
        result = await updateUser(initial.user_id, payload)
      } else {
        const payload: CreateAdminUserPayload = {
          ...basePayload,
          password,
        }
        result = await addUser(payload)
      }

      if (!result.success || !result.data?.user) {
        addToast({ type: 'error', message: result.message || (isEditMode ? '更新用户失败' : '创建用户失败') })
        return
      }

      addToast({ type: 'success', message: result.message || (isEditMode ? '用户更新成功' : '用户创建成功') })
      onSaved(toUser(result.data.user), isEditMode ? 'update' : 'create')
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, isEditMode ? '更新用户失败' : '创建用户失败') })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal-content max-w-2xl">
        <div className="modal-header">
          <h2 className="modal-title">{isEditMode ? '编辑用户' : '新增用户'}</h2>
          <button className="modal-close" onClick={onClose} disabled={saving}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="modal-body">
          {isCloudEdit && (
            <div className="mb-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-700 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-200">
              <div className="flex items-center gap-2 font-medium">
                <Cloud className="w-4 h-4" />
                云端统一认证账号
              </div>
              <div className="mt-1">
                该账号由云端统一认证服务管理，账号资料请在云端服务中修改；本页仅可查看，避免出现“本机改成功、云端未生效”的分裂状态。
              </div>
            </div>
          )}

          {isEditMode && initial && (
            <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm dark:border-slate-700 dark:bg-slate-800/60">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <div className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-200">
                    <ShieldCheck className="w-4 h-4" />
                    套餐：{initial.plan_code || 'NORMAL'}
                  </div>
                  <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    到期时间：{formatPlanExpiresAt(initial.plan_expires_at ?? initial.expire_at)}
                  </div>
                </div>
                {!initial.is_admin && onOpenEntitlements && (
                  <button
                    type="button"
                    className="btn-ios-secondary whitespace-nowrap"
                    onClick={() => onOpenEntitlements(initial)}
                  >
                    去套餐权限开通
                  </button>
                )}
              </div>
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                VIP 开通、套餐变更与功能授权已统一放到「套餐权限」页面，此窗口不再编辑套餐。
              </p>
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="input-group">
              <label className="input-label">用户名 <span className="text-red-500">*</span></label>
              <input
                className="input-ios"
                value={form.username}
                onChange={(event) => updateField('username', event.target.value)}
                placeholder="请输入用户名"
                maxLength={64}
                disabled={isCloudEdit}
              />
            </div>
            <div className="input-group">
              <label className="input-label">邮箱（可选）</label>
              <input
                className="input-ios"
                type="email"
                value={form.email}
                onChange={(event) => updateField('email', event.target.value)}
                placeholder="可选，不填写也可以"
                disabled={isCloudEdit}
              />
            </div>
            <div className="input-group">
              <label className="input-label">手机号</label>
              <input
                className="input-ios"
                value={form.phone}
                onChange={(event) => updateField('phone', event.target.value)}
                placeholder="请输入手机号"
                maxLength={32}
                disabled={isCloudEdit}
              />
            </div>
            <div className="input-group">
              <label className="input-label">角色 <span className="text-red-500">*</span></label>
              <select
                className="input-ios"
                value={form.role}
                onChange={(event) => updateField('role', event.target.value as UserRole)}
                disabled={isCloudEdit}
              >
                {roleOptions.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
            <div className="input-group">
              <label className="input-label">状态 <span className="text-red-500">*</span></label>
              <select
                className="input-ios"
                value={form.status}
                onChange={(event) => updateField('status', event.target.value as UserStatus)}
                disabled={isCloudEdit}
              >
                {statusOptions.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
            <div className="input-group">
              <label className="input-label">可添加账号数量</label>
              <input
                className="input-ios"
                type="number"
                min={1}
                step={1}
                value={form.account_limit}
                onChange={(event) => updateField('account_limit', event.target.value)}
                placeholder="留空表示不限制"
                disabled={isCloudEdit}
              />
              <p className="text-xs text-slate-400 mt-1">账号数量上限也可在「套餐权限」中按用户单独授权</p>
            </div>
            <div className="input-group">
              <label className="input-label">{isEditMode ? '新密码' : '登录密码'} {!isEditMode && <span className="text-red-500">*</span>}</label>
              <input
                className="input-ios"
                type="password"
                value={form.password}
                onChange={(event) => updateField('password', event.target.value)}
                placeholder={isEditMode ? '不填写则不修改' : '请输入登录密码'}
                maxLength={128}
                disabled={isCloudEdit}
              />
            </div>
            <div className="input-group">
              <label className="input-label">确认密码 {(form.password || !isEditMode) && <span className="text-red-500">*</span>}</label>
              <input
                className="input-ios"
                type="password"
                value={form.confirmPassword}
                onChange={(event) => updateField('confirmPassword', event.target.value)}
                placeholder={isEditMode ? '如填写了新密码，请再次输入' : '请再次输入登录密码'}
                maxLength={128}
                disabled={isCloudEdit}
              />
            </div>
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn-ios-secondary" onClick={onClose} disabled={saving}>取消</button>
          {!isCloudEdit && (
            <button className="btn-ios-primary" onClick={handleSave} disabled={saving}>
              {saving && <Loader2 className="w-4 h-4 animate-spin" />}
              {isEditMode ? '保存修改' : '创建用户'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default UserFormModal
