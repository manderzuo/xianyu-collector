import { useEffect, useState } from 'react'
import { get } from '../api/client'

type Metrics = { accounts: number; products: number; orders: number; messages: number; publish_jobs: number; risk_events: number }
const emptyMetrics: Metrics = { accounts: 0, products: 0, orders: 0, messages: 0, publish_jobs: 0, risk_events: 0 }

export default function Dashboard() {
  const [health, setHealth] = useState('检查中')
  const [metrics, setMetrics] = useState(emptyMetrics)
  const [taskCount, setTaskCount] = useState(0)
  useEffect(() => {
    Promise.all([get('/health'), get<{ metrics: Metrics }>('/data-analysis'), get<{ total: number }>('/scheduled-tasks')])
      .then(([, analysis, tasks]) => { setHealth('服务正常'); setMetrics({ ...emptyMetrics, ...(analysis.data?.metrics || {}) }); setTaskCount(tasks.data?.total || 0) })
      .catch(() => setHealth('待连接'))
  }, [])
  const cards = [{ label: '活跃账号', value: String(metrics.accounts), note: metrics.accounts ? '已接入工作区' : '等待接入账号' }, { label: '在售商品', value: String(metrics.products), note: '来自商品数据表' }, { label: '待处理消息', value: String(metrics.messages), note: '消息流水实时汇总' }, { label: '运行任务', value: String(taskCount), note: '任务注册表在线' }]
  return <div><div className="page-heading"><div><p className="eyebrow">OVERVIEW / 今日概览</p><h1>运营工作台</h1><p className="muted">把需要关注的事情，集中在一个清晰的视图里。</p></div><span className="status-pill"><i />{health}</span></div><div className="stats-grid">{cards.map((card) => <div className="stat-card" key={card.label}><p>{card.label}</p><strong>{card.value}</strong><small>{card.note}</small></div>)}</div><div className="dashboard-grid"><section className="panel"><div className="panel-head"><div><p className="eyebrow">QUICK START</p><h2>从这里开始</h2></div></div><div className="quick-grid"><a href="/accounts"><b>接入账号</b><span>添加账号并维护登录态</span></a><a href="/materials"><b>准备素材</b><span>建立可复用的商品素材库</span></a><a href="/publish"><b>发布商品</b><span>单发或批量安排发布</span></a><a href="/scheduled-tasks"><b>检查任务</b><span>查看并手动触发自动化任务</span></a></div></section><section className="panel notice-panel"><p className="eyebrow">SYSTEM NOTE</p><h2>数据链路已接通</h2><p className="muted">账号、商品、订单、消息和风控指标均从当前工作区数据库实时汇总。外部平台动作会根据连接配置执行并记录结果。</p><div className="progress-line"><span style={{ width: '100%' }} /></div><small>基础数据链路已完成</small></section></div></div>
}
