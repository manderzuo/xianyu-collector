import { useEffect, useState } from 'react'
import { get, post } from '../api/client'

type QrSession = {
  session_id: string
  status: string
  qr_code_url?: string
  verification_url?: string | null
  face_qr_url?: string | null
  account_id?: number | null
  is_new_account?: boolean | null
  error?: string | null
  runtime?: { status?: string }
}

const labels: Record<string, string> = {
  waiting: '等待扫码', scanned: '已扫码，等待确认', success: '登录成功',
  processing: '正在验证登录态',
  verification_required: '需要手机人脸核验', expired: '二维码已过期',
  cancelled: '已取消', failed: '登录失败', not_found: '会话不存在',
}

export default function QrLoginPage() {
  const [session, setSession] = useState<QrSession | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function generate() {
    setBusy(true); setError(''); setNotice('')
    try {
      if (session && ['waiting', 'scanned', 'verification_required'].includes(session.status)) {
        await post(`/qr-login/cancel/${session.session_id}`)
      }
      const result = await post<QrSession>('/qr-login/generate', {})
      setSession(result.data); setNotice('二维码已生成，请使用闲鱼 App 扫码')
    } catch (err) { setError(err instanceof Error ? err.message : '生成二维码失败') }
    finally { setBusy(false) }
  }

  async function cancel() {
    if (!session) return
    try { await post(`/qr-login/cancel/${session.session_id}`); setSession({ ...session, status: 'cancelled' }); setNotice('扫码会话已取消') }
    catch (err) { setError(err instanceof Error ? err.message : '取消失败') }
  }

  useEffect(() => {
    if (!session || !['waiting', 'scanned', 'verification_required', 'processing'].includes(session.status)) return
    let stopped = false
    const poll = async () => {
      try {
        const result = await get<QrSession>(`/qr-login/status/${session.session_id}`)
        if (!stopped) setSession((current) => current ? { ...current, ...result.data } : result.data)
      } catch (err) { if (!stopped) setError(err instanceof Error ? err.message : '查询扫码状态失败') }
    }
    void poll()
    const timer = window.setInterval(() => { void poll() }, 1000)
    return () => { stopped = true; window.clearInterval(timer) }
  }, [session?.session_id, session?.status])

  const status = session ? (labels[session.status] || session.status) : '尚未生成'
  const active = session && ['waiting', 'scanned', 'verification_required'].includes(session.status)

  return <div className="qr-page">
    <div className="page-heading"><div><p className="eyebrow">ACCOUNT / QR LOGIN</p><h1><span className="title-icon">▦</span>扫码登录</h1><p className="muted">使用闲鱼 App 扫码，登录成功后会自动创建或更新账号并加载登录态。</p></div><div className="heading-actions"><button className="secondary" onClick={() => void generate()} disabled={busy}>{busy ? '生成中…' : '重新生成二维码'}</button></div></div>
    {notice && <div className="toast">{notice}</div>}
    {error && <div className="inline-error standalone-error">{error}</div>}
    <section className="panel qr-panel">
      <div className="qr-card">
        {session?.qr_code_url && active ? <img className="qr-image" src={session.qr_code_url} alt="闲鱼扫码登录二维码" /> : <div className="qr-empty">▦</div>}
        <div className={`qr-status qr-status-${session?.status || 'empty'}`}><span />{status}</div>
        {session?.status === 'scanned' && <p className="muted">请在手机闲鱼中确认登录。</p>}
        {session?.status === 'verification_required' && <><p className="muted">该账号需要额外的人脸核验，请按手机提示完成。</p>{session.face_qr_url && <img className="qr-image face-qr-image" src={session.face_qr_url} alt="人脸核验二维码" />}{session.verification_url && <a href={session.verification_url} target="_blank" rel="noreferrer">打开核验页面</a>}</>}
        {session?.status === 'processing' && <p className="muted">登录信息已保存，正在验证 Token 并建立在线连接，请稍候。</p>}
        {session?.status === 'success' && <div className="qr-success"><strong>账号已接入并在线</strong><span>账号 ID：{session.account_id}</span><span>{session.is_new_account ? '已创建新账号' : '已更新原账号登录态'}</span>{session.runtime?.status && <span>连接服务：{session.runtime.status}</span>}</div>}
        {session?.error && <p className="error-text">{session.error}</p>}
        <div className="qr-actions"><button className="primary" onClick={() => void generate()} disabled={busy}>{session ? '生成新的二维码' : '生成登录二维码'}</button>{active && <button className="secondary" onClick={() => void cancel()}>取消本次登录</button>}</div>
      </div>
      <div className="qr-guide"><p className="eyebrow">HOW IT WORKS</p><h2>扫码后自动完成三件事</h2><div><b>01 / 登录确认</b><span>服务端持续查询二维码状态，识别扫码与确认。</span></div><div><b>02 / 登录态保存</b><span>成功后保存 Cookie、平台账号标识和登录记录。</span></div><div><b>03 / 账号接入</b><span>通知连接服务加载最新登录态，账号进入可用状态。</span></div><small>二维码有效期约 5 分钟；超时后请重新生成。</small></div>
    </section>
  </div>
}
