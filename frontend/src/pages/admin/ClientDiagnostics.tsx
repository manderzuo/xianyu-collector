import { useCallback, useEffect, useState } from 'react'
import { CheckCircle2, Download, Eye, FileWarning, RefreshCw, Trash2, Wrench } from 'lucide-react'
import {
  deleteClientDiagnostic,
  downloadClientDiagnostic,
  getClientDiagnostic,
  getClientDiagnosticStats,
  getClientDiagnostics,
  updateClientDiagnosticStatus,
  type ClientDiagnosticReport,
  type DiagnosticSeverity,
  type DiagnosticStatus,
} from '@/api/clientDiagnostics'
import { useUIStore } from '@/store/uiStore'
import { useAuthStore } from '@/store/authStore'
import { PageLoading } from '@/components/common/Loading'
import { cn } from '@/utils/cn'

const statusLabels: Record<DiagnosticStatus, string> = { pending: '待处理', processing: '处理中', resolved: '已解决', ignored: '已忽略' }
const severityLabels: Record<DiagnosticSeverity, string> = { info: '信息', warning: '警告', error: '错误', critical: '严重' }

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function statusClass(status: DiagnosticStatus) {
  return status === 'resolved' ? 'badge-success' : status === 'processing' ? 'badge-warning' : status === 'ignored' ? 'badge-gray' : 'badge-danger'
}

export function ClientDiagnostics() {
  const { addToast } = useUIStore()
  const { isAuthenticated, token, _hasHydrated } = useAuthStore()
  const [reports, setReports] = useState<ClientDiagnosticReport[]>([])
  const [stats, setStats] = useState({ total: 0, pending: 0, processing: 0, resolved: 0, ignored: 0, last_24_hours: 0 })
  const [status, setStatus] = useState<DiagnosticStatus | ''>('')
  const [severity, setSeverity] = useState<DiagnosticSeverity | ''>('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const [totalReports, setTotalReports] = useState(0)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<ClientDiagnosticReport | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async (showLoading = true) => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    if (showLoading) setLoading(true)
    try {
      const [listResult, statsResult] = await Promise.all([
        getClientDiagnostics({ status, severity, search, limit: 50, offset: page * 50 }),
        getClientDiagnosticStats(),
      ])
      if (listResult.success) {
        setReports(listResult.data?.items || [])
        setTotalReports(listResult.data?.total || 0)
      }
      if (statsResult.success && statsResult.data) setStats(statsResult.data)
    } catch {
      if (showLoading) addToast({ type: 'error', message: '加载客户端错误日志失败' })
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [_hasHydrated, isAuthenticated, token, status, severity, search, page, addToast])

  useEffect(() => { load() }, [load])
  useEffect(() => { setPage(0) }, [status, severity, search])
  useEffect(() => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    const timer = window.setInterval(() => { load(false) }, 15000)
    return () => window.clearInterval(timer)
  }, [_hasHydrated, isAuthenticated, token, load])

  const showDetail = async (report: ClientDiagnosticReport) => {
    setSelected(report)
    setDetailLoading(true)
    try {
      const result = await getClientDiagnostic(report.id)
      if (result.success && result.data) setSelected(result.data)
    } catch { addToast({ type: 'error', message: '加载诊断详情失败' }) }
    finally { setDetailLoading(false) }
  }

  const download = async (report: ClientDiagnosticReport) => {
    const result = await downloadClientDiagnostic(report.id)
    if (!result.success || !result.blob) { addToast({ type: 'error', message: result.message || '下载失败' }); return }
    const url = URL.createObjectURL(result.blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = result.filename || `${report.report_code}.zip`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const updateStatus = async (report: ClientDiagnosticReport, next: DiagnosticStatus) => {
    try {
      const result = await updateClientDiagnosticStatus(report.id, next)
      if (!result.success) throw new Error(result.message || '状态更新失败')
      addToast({ type: 'success', message: '诊断报告状态已更新' })
      await load(false)
      if (selected?.id === report.id && result.data) setSelected(result.data)
    } catch (error) { addToast({ type: 'error', message: error instanceof Error ? error.message : '状态更新失败' }) }
  }

  const remove = async (report: ClientDiagnosticReport) => {
    if (!window.confirm(`确认删除诊断报告 ${report.report_code}？删除后无法恢复。`)) return
    try {
      const result = await deleteClientDiagnostic(report.id)
      if (!result.success) throw new Error(result.message || '删除失败')
      setSelected((current) => current?.id === report.id ? null : current)
      addToast({ type: 'success', message: '诊断报告已删除' })
      await load(false)
    } catch (error) { addToast({ type: 'error', message: error instanceof Error ? error.message : '删除失败' }) }
  }

  if (loading && reports.length === 0) return <PageLoading />

  return (
    <div className="space-y-4">
      <div className="page-header flex-between flex-wrap gap-4">
        <div>
          <h1 className="page-title flex items-center gap-2"><FileWarning className="w-5 h-5 text-amber-500" />客户端错误日志</h1>
          <p className="page-description">查看各电脑上传的安装、更新和运行诊断报告。日志按用户隔离，文件仅管理员可下载。</p>
        </div>
        <button onClick={() => load()} disabled={loading} className="btn-ios-secondary"><RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />刷新</button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        {[
          ['全部报告', stats.total, 'text-slate-700 dark:text-slate-100'],
          ['待处理', stats.pending, 'text-red-500'],
          ['处理中', stats.processing, 'text-amber-500'],
          ['已解决', stats.resolved, 'text-emerald-500'],
          ['已忽略', stats.ignored, 'text-slate-400'],
          ['近24小时', stats.last_24_hours, 'text-blue-500'],
        ].map(([label, value, color]) => <div key={String(label)} className="vben-card px-4 py-3"><p className="text-xs text-slate-500 dark:text-slate-400">{label}</p><p className={cn('mt-1 text-xl font-semibold', color)}>{value}</p></div>)}
      </div>

      <div className="vben-card p-4 flex flex-wrap items-center gap-3">
        <select value={status} onChange={(event) => setStatus(event.target.value as DiagnosticStatus | '')} className="input-ios w-auto"><option value="">全部状态</option>{Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
        <select value={severity} onChange={(event) => setSeverity(event.target.value as DiagnosticSeverity | '')} className="input-ios w-auto"><option value="">全部级别</option>{Object.entries(severityLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索报告编号、用户、版本或摘要" className="input-ios min-w-[240px] flex-1" />
      </div>

      <div className="vben-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1050px] text-sm">
            <thead className="bg-slate-50 dark:bg-slate-800/60"><tr>{['报告编号', '用户 / 设备', '版本 / 阶段', '摘要', '级别', '状态', '提交时间', '操作'].map((heading) => <th key={heading} className="px-4 py-3 text-left font-medium text-slate-500 dark:text-slate-300">{heading}</th>)}</tr></thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
              {reports.length === 0 ? <tr><td colSpan={8} className="px-6 py-14 text-center text-slate-500">暂无客户端错误日志</td></tr> : reports.map((report) => (
                <tr key={report.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className="px-4 py-3"><button onClick={() => showDetail(report)} className="font-medium text-blue-500 hover:underline">{report.report_code}</button><div className="mt-1 text-xs text-slate-400">{formatBytes(report.size_bytes)}</div></td>
                  <td className="px-4 py-3"><div className="text-slate-700 dark:text-slate-200">{report.owner_username || '未登录设备'}</div><div className="mt-1 text-xs text-slate-400">设备 {report.device_hash || '-'}</div></td>
                  <td className="px-4 py-3"><div className="text-slate-700 dark:text-slate-200">v{report.version || '-'}</div><div className="mt-1 text-xs text-slate-400">{report.stage || '未知阶段'}</div></td>
                  <td className="max-w-[300px] px-4 py-3 truncate text-slate-600 dark:text-slate-300" title={report.summary}>{report.summary}</td>
                  <td className="px-4 py-3"><span className={cn('badge', report.severity === 'critical' || report.severity === 'error' ? 'badge-danger' : report.severity === 'warning' ? 'badge-warning' : 'badge-info')}>{severityLabels[report.severity] || report.severity}</span></td>
                  <td className="px-4 py-3"><span className={cn('badge', statusClass(report.status))}>{statusLabels[report.status] || report.status}</span></td>
                  <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-500">{new Date(report.created_at).toLocaleString()}</td>
                  <td className="px-4 py-3"><div className="flex items-center gap-1.5"><button title="查看详情" onClick={() => showDetail(report)} className="icon-btn text-blue-500"><Eye className="w-4 h-4" /></button><button title="下载日志" onClick={() => download(report)} className="icon-btn text-cyan-500"><Download className="w-4 h-4" /></button>{report.status !== 'resolved' && <button title="标记已解决" onClick={() => updateStatus(report, 'resolved')} className="icon-btn text-emerald-500"><CheckCircle2 className="w-4 h-4" /></button>}{report.status === 'pending' && <button title="标记处理中" onClick={() => updateStatus(report, 'processing')} className="icon-btn text-amber-500"><Wrench className="w-4 h-4" /></button>}<button title="删除" onClick={() => remove(report)} className="icon-btn text-red-500"><Trash2 className="w-4 h-4" /></button></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {totalReports > 50 && <div className="flex items-center justify-between text-sm text-slate-500"><span>第 {page + 1} / {Math.ceil(totalReports / 50)} 页，共 {totalReports} 份报告</span><div className="flex gap-2"><button onClick={() => setPage((current) => Math.max(0, current - 1))} disabled={page === 0 || loading} className="btn-ios-secondary">上一页</button><button onClick={() => setPage((current) => current + 1)} disabled={page + 1 >= Math.ceil(totalReports / 50) || loading} className="btn-ios-secondary">下一页</button></div></div>}

      {selected && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/50 p-4" onClick={() => setSelected(null)}><div className="w-full max-w-3xl rounded-xl bg-white shadow-2xl dark:bg-slate-900" onClick={(event) => event.stopPropagation()}><div className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-700"><div><h2 className="font-semibold text-slate-900 dark:text-white">诊断报告 {selected.report_code}</h2><p className="mt-1 text-xs text-slate-500">{detailLoading ? '正在读取详情...' : selected.summary}</p></div><button onClick={() => setSelected(null)} className="text-2xl leading-none text-slate-400 hover:text-slate-700">×</button></div><div className="grid max-h-[65vh] gap-4 overflow-y-auto p-5 md:grid-cols-2"><div className="space-y-3 text-sm"><p><span className="text-slate-500">用户：</span>{selected.owner_username || '未登录设备'}</p><p><span className="text-slate-500">设备：</span>{selected.device_hash || '-'}</p><p><span className="text-slate-500">版本：</span>v{selected.version || '-'} / {selected.build || '-'}</p><p><span className="text-slate-500">阶段：</span>{selected.stage || '-'}</p><p><span className="text-slate-500">提交时间：</span>{new Date(selected.created_at).toLocaleString()}</p><p><span className="text-slate-500">过期时间：</span>{selected.expires_at ? new Date(selected.expires_at).toLocaleString() : '-'}</p><p><span className="text-slate-500">SHA256：</span><span className="break-all font-mono text-xs">{selected.sha256}</span></p></div><pre className="rounded-lg bg-slate-950 p-4 text-xs leading-5 text-slate-200">{JSON.stringify(selected.metadata || {}, null, 2)}</pre></div><div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 px-5 py-4 dark:border-slate-700"><button onClick={() => download(selected)} className="btn-ios-secondary"><Download className="w-4 h-4" />下载日志</button>{selected.status !== 'resolved' && <button onClick={() => updateStatus(selected, 'resolved')} className="btn-ios-primary"><CheckCircle2 className="w-4 h-4" />标记已解决</button>}</div></div></div>}
    </div>
  )
}
