import { useEffect, useState } from 'react'
import { get, post } from '../api/client'

type Entry = { id: number; title: string; category?: string; score: number; source?: string; status: string }

export default function RankingPage() {
  const [items, setItems] = useState<Entry[]>([])
  const [title, setTitle] = useState('')
  const [score, setScore] = useState('0')
  const [category, setCategory] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  async function load() {
    try { const result = await get<{ items: Entry[] }>('/ranking'); setItems(result.data?.items || []); setError('') }
    catch (err) { setError(err instanceof Error ? err.message : '排名加载失败') }
  }
  useEffect(() => { void load() }, [])

  async function addEntry() {
    if (!title.trim()) { setError('请填写商品标题'); return }
    try {
      const result = await post('/ranking', { title: title.trim(), category: category || null, score: Number(score) || 0, source: 'manual' })
      setMessage(result.message); setTitle(''); setCategory(''); setScore('0'); await load()
    } catch (err) { setError(err instanceof Error ? err.message : '排名记录保存失败') }
  }

  return <div><div className="page-heading"><div><p className="eyebrow">GROWTH / PRODUCT RANKING</p><h1><span className="title-icon">↗</span>新品排名</h1><p className="muted">根据评分记录排序，采集任务或人工复核都可以写入同一套排名数据。</p></div><button className="secondary" onClick={() => void load()}>刷新数据</button></div>{message && <div className="toast">{message}</div>}{error && <div className="inline-error standalone-error">{error}</div>}<section className="panel ranking-panel"><div className="ranking-form"><label>商品标题<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="例如：复古帆布包" /></label><label>分类<input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="可选" /></label><label>评分<input type="number" min="0" step="0.01" value={score} onChange={(e) => setScore(e.target.value)} /></label><button className="primary" onClick={() => void addEntry()}>添加排名记录</button></div><div className="ranking-list"><div className="ranking-list-head"><span>排名</span><span>商品</span><span>分类</span><span>评分</span><span>来源</span></div>{items.length ? items.map((item, index) => <div className="ranking-list-row" key={item.id}><strong>#{index + 1}</strong><span>{item.title}</span><span>{item.category || '—'}</span><b>{Number(item.score).toFixed(2)}</b><span>{item.source || '—'}</span></div>) : <div className="empty-state"><div className="empty-symbol">↗</div><strong>还没有排名数据</strong><p>添加第一条商品评分后，排名会按评分自动排序。</p></div>}</div></section></div>
}
