import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { post } from '../api/client'
import { BRAND_NAME } from '../config'
import { useAuth } from '../store/auth'

export default function Login() {
  const navigate = useNavigate(); const setAuth = useAuth((s) => s.setAuth)
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [loading, setLoading] = useState(false)
  async function submit(event: FormEvent) { event.preventDefault(); setError(''); setLoading(true); try { const result = await post<{ access_token: string; user: { id: number; username: string; nickname: string; role: string } }>('/auth/login', { username, password }); setAuth(result.data.access_token, result.data.user); navigate('/') } catch (e) { setError(e instanceof Error ? e.message : '登录失败') } finally { setLoading(false) } }
  return <main className="login-shell"><section className="login-card"><div className="brand-mark">XR</div><p className="eyebrow">{BRAND_NAME} WORKSPACE</p><h1>欢迎回来</h1><p className="muted">统一管理账号、商品、消息与运营任务</p><form onSubmit={submit}><label>用户名<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label><label>密码<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>{error && <div className="error-text">{error}</div>}<button className="primary wide" disabled={loading}>{loading ? '正在登录…' : '进入工作台'}</button></form></section><div className="login-art"><span>01</span><h2>把重复工作<br />交给系统。</h2><p>从消息响应到商品运营，用清晰的工作流保持每个账号都在掌控之中。</p></div></main>
}
