import { useEffect, useState } from 'react'
import { Copy, Loader2, RefreshCw, ShieldCheck, X } from 'lucide-react'
import {
  createRegistrationInvites,
  getRegistrationInvites,
  revokeRegistrationInvite,
  type RegistrationInvite,
  type RegistrationInviteStatus,
} from '@/api/admin'
import { useUIStore } from '@/store/uiStore'
import { copyToClipboard } from '@/utils/clipboard'
import { formatDateTime } from '@/utils/date'
import { getApiErrorMessage } from '@/utils/request'

interface Props {
  onClose: () => void
}

const statusLabels: Record<RegistrationInviteStatus, string> = {
  active: '未使用',
  used: '已使用',
  revoked: '已撤销',
  expired: '已过期',
}

const statusClasses: Record<RegistrationInviteStatus, string> = {
  active: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  used: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  revoked: 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
  expired: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
}

export function RegistrationInviteModal({ onClose }: Props) {
  const { addToast } = useUIStore()
  const [invites, setInvites] = useState<RegistrationInvite[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [count, setCount] = useState('1')
  const [expiresAt, setExpiresAt] = useState('')
  const [note, setNote] = useState('')
  const [generatedCodes, setGeneratedCodes] = useState<string[]>([])

  const loadInvites = async () => {
    setLoading(true)
    try {
      const result = await getRegistrationInvites({ limit: 100 })
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '加载邀请码失败' })
        return
      }
      setInvites(result.data)
      setTotal(result.total)
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '加载邀请码失败') })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadInvites()
  }, [])

  const handleGenerate = async () => {
    const amount = Number(count)
    if (!Number.isInteger(amount) || amount < 1 || amount > 100) {
      addToast({ type: 'warning', message: '生成数量必须是 1-100 的整数' })
      return
    }
    setSaving(true)
    try {
      const result = await createRegistrationInvites({
        count: amount,
        expires_at: expiresAt || null,
        note: note.trim(),
      })
      if (!result.success || !result.data) {
        addToast({ type: 'error', message: result.message || '生成邀请码失败' })
        return
      }
      setGeneratedCodes(result.data.codes || [])
      setNote('')
      addToast({ type: 'success', message: result.message || `已生成 ${amount} 个邀请码` })
      await loadInvites()
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '生成邀请码失败') })
    } finally {
      setSaving(false)
    }
  }

  const handleCopy = async (value: string, message = '邀请码已复制') => {
    const copied = await copyToClipboard(value)
    addToast({ type: copied ? 'success' : 'error', message: copied ? message : '复制失败，请手动选择文本复制' })
  }

  const handleRevoke = async (item: RegistrationInvite) => {
    if (item.status !== 'active' || !window.confirm(`确定撤销邀请码 ${item.code} 吗？撤销后不能继续注册。`)) return
    try {
      const result = await revokeRegistrationInvite(item.id)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '撤销邀请码失败' })
        return
      }
      addToast({ type: 'success', message: result.message || '邀请码已撤销' })
      await loadInvites()
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '撤销邀请码失败') })
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal-content max-w-5xl max-h-[92vh] overflow-hidden flex flex-col">
        <div className="modal-header flex-shrink-0">
          <div>
            <h2 className="modal-title flex items-center gap-2"><ShieldCheck className="w-5 h-5 text-blue-500" />邀请码管理</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">邀请码由管理员发放，一次性使用，可设置过期时间或提前撤销。</p>
          </div>
          <button className="modal-close" onClick={onClose} disabled={saving}><X className="w-5 h-5" /></button>
        </div>

        <div className="flex-1 overflow-auto p-6 space-y-5">
          <div className="rounded-xl border border-blue-100 dark:border-blue-900/40 bg-blue-50/70 dark:bg-blue-900/10 p-4">
            <div className="grid grid-cols-1 md:grid-cols-[120px_220px_1fr_auto] gap-3 items-end">
              <div className="input-group">
                <label className="input-label">生成数量</label>
                <input type="number" min={1} max={100} value={count} onChange={(e) => setCount(e.target.value)} className="input-ios" />
              </div>
              <div className="input-group">
                <label className="input-label">过期时间（可选）</label>
                <input type="datetime-local" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} className="input-ios" />
              </div>
              <div className="input-group">
                <label className="input-label">备注（可选）</label>
                <input type="text" maxLength={255} value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：9月客户 / 测试用户" className="input-ios" />
              </div>
              <button type="button" onClick={handleGenerate} disabled={saving} className="btn-ios-primary whitespace-nowrap">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                生成邀请码
              </button>
            </div>
          </div>

          {generatedCodes.length > 0 && (
            <div className="rounded-xl border border-emerald-200 dark:border-emerald-900/40 bg-emerald-50/70 dark:bg-emerald-900/10 p-4">
              <div className="flex items-center justify-between gap-3 mb-3">
                <div>
                  <h3 className="font-medium text-emerald-800 dark:text-emerald-200">本次生成的完整邀请码</h3>
                  <p className="text-xs text-emerald-700/80 dark:text-emerald-300/80 mt-1">完整码会安全保存，关闭窗口后也可在记录列表中复制。</p>
                </div>
                <button type="button" onClick={() => handleCopy(generatedCodes.join('\n'), '全部邀请码已复制')} className="btn-ios-secondary">
                  <Copy className="w-4 h-4" />复制全部
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {generatedCodes.map((code) => (
                  <div key={code} className="flex items-center justify-between gap-2 rounded-lg bg-white/80 dark:bg-slate-800/70 px-3 py-2">
                    <code className="font-mono text-sm tracking-wide text-slate-800 dark:text-slate-100">{code}</code>
                    <button type="button" onClick={() => handleCopy(code)} className="p-1.5 text-slate-500 hover:text-blue-600" title="复制邀请码"><Copy className="w-4 h-4" /></button>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between">
            <h3 className="font-medium text-slate-900 dark:text-slate-100">邀请码记录 <span className="text-sm font-normal text-slate-500">共 {total} 个</span></h3>
            <button type="button" onClick={() => void loadInvites()} disabled={loading} className="btn-ios-secondary">
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}刷新
            </button>
          </div>

          <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
            <table className="table-ios">
              <thead><tr><th>邀请码</th><th>状态</th><th>备注</th><th>过期时间</th><th>使用时间</th><th>操作</th></tr></thead>
              <tbody>
                {loading && invites.length === 0 ? (
                  <tr><td colSpan={6} className="text-center py-8"><Loader2 className="w-5 h-5 animate-spin mx-auto text-blue-500" /></td></tr>
                ) : invites.length === 0 ? (
                  <tr><td colSpan={6} className="text-center py-8 text-slate-500">暂无邀请码，请先生成。</td></tr>
                ) : invites.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <div className="flex items-center gap-2">
                        <code className="font-mono text-sm">{item.code}</code>
                        {item.code_available ? (
                          <button type="button" onClick={() => void handleCopy(item.code)} className="p-1 text-slate-500 hover:text-blue-600 dark:text-slate-400 dark:hover:text-blue-400" title="复制邀请码" aria-label="复制邀请码">
                            <Copy className="w-4 h-4" />
                          </button>
                        ) : (
                          <span className="p-1 text-slate-300 dark:text-slate-600" title="该历史邀请码未保存完整码，无法复制" aria-label="该历史邀请码未保存完整码，无法复制">
                            <Copy className="w-4 h-4" />
                          </span>
                        )}
                      </div>
                    </td>
                    <td><span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ${statusClasses[item.status]}`}>{statusLabels[item.status]}</span></td>
                    <td className="text-slate-500 dark:text-slate-400">{item.note || '-'}</td>
                    <td className="whitespace-nowrap text-sm text-slate-500 dark:text-slate-400">{formatDateTime(item.expires_at)}</td>
                    <td className="whitespace-nowrap text-sm text-slate-500 dark:text-slate-400">{formatDateTime(item.used_at)}</td>
                    <td>
                      {item.status === 'active' ? <button type="button" onClick={() => void handleRevoke(item)} className="text-red-500 hover:text-red-700 text-sm">撤销</button> : <span className="text-slate-400 text-sm">-</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="modal-footer flex-shrink-0"><button type="button" className="btn-ios-secondary" onClick={onClose}>关闭</button></div>
      </div>
    </div>
  )
}
