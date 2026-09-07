import { useEffect, useState } from 'react'
import { listAdminPlans, updatePlanFeature, type AdminPlan } from '@/api/entitlements'

const FEATURE_LABELS: Record<string, string> = {
  'account.manage': '闲鱼账号',
  'card.auto_delivery': '自动发货卡券',
  'product.auto_publish': '自动发布商品',
  'ai.smart_reply': 'AI 智能回复',
  'keyword.reply': '关键词回复',
}

export function Entitlements() {
  const [plans, setPlans] = useState<AdminPlan[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<string | null>(null)
  const [message, setMessage] = useState('')

  const reload = async () => {
    setLoading(true)
    try {
      const result = await listAdminPlans()
      setPlans(result.data?.items || [])
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '套餐加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [])

  const saveFeature = async (plan: AdminPlan, featureKey: string, feature: AdminPlan['features'][string]) => {
    const key = `${plan.code}:${featureKey}`
    setSaving(key)
    setMessage('')
    try {
      await updatePlanFeature(plan.code, featureKey, {
        enabled: feature.enabled,
        limit: feature.unlimited ? null : feature.limit,
        unlimited: feature.unlimited,
      })
      setMessage(`${plan.name} / ${FEATURE_LABELS[featureKey] || featureKey} 已保存`)
      await reload()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败')
    } finally {
      setSaving(null)
    }
  }

  if (loading) return <div className="p-6 text-slate-500">正在加载套餐配置…</div>

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-white">套餐与功能权限</h1>
        <p className="mt-1 text-sm text-slate-500">修改套餐默认授权；单用户覆盖请通过管理员接口或用户管理页配置。</p>
      </div>
      {message && <div className="rounded-lg bg-blue-50 px-4 py-3 text-sm text-blue-700">{message}</div>}
      {plans.map((plan) => (
        <section key={plan.code} className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="font-semibold text-slate-900 dark:text-white">{plan.name} ({plan.code})</h2>
              <span className="text-xs text-slate-500">{plan.status === 'active' ? '启用中' : '已停用'}</span>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead><tr className="border-b text-left text-slate-500"><th className="px-2 py-2">功能</th><th className="px-2 py-2">启用</th><th className="px-2 py-2">配额</th><th className="px-2 py-2">操作</th></tr></thead>
              <tbody>
                {Object.entries(plan.features).map(([featureKey, feature]) => {
                  const saveKey = `${plan.code}:${featureKey}`
                  return (
                    <tr key={featureKey} className="border-b last:border-0">
                      <td className="px-2 py-3">{FEATURE_LABELS[featureKey] || featureKey}</td>
                      <td className="px-2 py-3"><input type="checkbox" checked={feature.enabled} onChange={(event) => setPlans((current) => current.map((item) => item.code !== plan.code ? item : { ...item, features: { ...item.features, [featureKey]: { ...feature, enabled: event.target.checked } } }))} /></td>
                      <td className="px-2 py-3"><input className="w-24 rounded border border-slate-300 bg-white px-2 py-1 text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100 dark:placeholder:text-slate-400" type="number" min={0} disabled={feature.unlimited} value={feature.unlimited ? '' : (feature.limit ?? '')} placeholder={feature.unlimited ? '无限制' : ''} onChange={(event) => setPlans((current) => current.map((item) => item.code !== plan.code ? item : { ...item, features: { ...item.features, [featureKey]: { ...feature, limit: event.target.value === '' ? null : Number(event.target.value) } } }))} /></td>
                      <td className="px-2 py-3 space-x-2"><label className="text-xs"><input type="checkbox" checked={feature.unlimited} onChange={(event) => setPlans((current) => current.map((item) => item.code !== plan.code ? item : { ...item, features: { ...item.features, [featureKey]: { ...feature, unlimited: event.target.checked } } }))} /> 无限</label><button className="rounded bg-blue-600 px-3 py-1 text-xs text-white disabled:opacity-50" disabled={saving === saveKey} onClick={() => void saveFeature(plan, featureKey, feature)}>{saving === saveKey ? '保存中' : '保存'}</button></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  )
}
