import { useEffect, useState } from 'react'
import { get, post } from '../api/client'

type Task = { name: string; display_name: string; cron: string; enabled: boolean; last_status?: string; last_run_at?: string | null }

export default function ScheduledTasks() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [running, setRunning] = useState('')

  async function load() {
    try { const result = await get<{ items: Task[] }>('/scheduled-tasks'); setTasks(result.data?.items || []); setError('') }
    catch (err) { setError(err instanceof Error ? err.message : '任务加载失败') }
  }
  useEffect(() => { void load() }, [])

  async function trigger(name: string) {
    setRunning(name); setError('')
    try { const result = await post<{ detail?: string }>(`/scheduled-tasks/${name}/trigger`); setMessage(result.data?.detail ? `${result.message}：${result.data.detail}` : result.message); await load() }
    catch (err) { setError(err instanceof Error ? err.message : '任务执行失败') }
    finally { setRunning('') }
  }

  return <div><div className="page-heading"><div><p className="eyebrow">AUTOMATION / TASK CENTER</p><h1>定时任务</h1><p className="muted">任务由独立调度服务按计划执行，也可以在这里立即触发。</p></div><div className="heading-actions"><button className="secondary" onClick={() => void load()}>刷新任务</button><span className="status-pill"><i />{tasks.length} 个任务在线</span></div></div>{message && <div className="toast">{message}</div>}{error && <div className="inline-error standalone-error">{error}</div>}<section className="panel table-panel"><div className="toolbar"><div><h2>任务注册表</h2><p className="muted">执行结果和最后运行时间会持久化到任务记录。</p></div></div><div className="task-list">{tasks.map((task) => <div className="task-row" key={task.name}><div className="task-dot" /><div className="task-main"><b>{task.display_name}</b><small>{task.name} · {task.cron} · {task.last_run_at ? new Date(task.last_run_at).toLocaleString() : '尚未执行'}</small></div><span className="tag">{task.last_status || 'idle'}</span><button className="text-button" disabled={running === task.name} onClick={() => void trigger(task.name)}>{running === task.name ? '执行中…' : '立即执行'}</button></div>)}</div></section></div>
}
