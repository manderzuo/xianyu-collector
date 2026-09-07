import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { MessageSquare, User, Lock, KeyRound, Eye, EyeOff } from 'lucide-react'
import { AuthNavbar } from '@/components/common/AuthNavbar'
import { SafeHtml } from '@/components/common/SafeHtml'
import { getDefaultAuthFooterAdSettings } from '@/api/settings'
import { register, getRegistrationStatus, getAuthFooterAdSettings } from '@/api/auth'
import { useUIStore } from '@/store/uiStore'
import { ButtonLoading } from '@/components/common/Loading'

export function Register() {
  const navigate = useNavigate()
  const { addToast } = useUIStore()

  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [registrationEnabled, setRegistrationEnabled] = useState(true)
  const [authFooterAd, setAuthFooterAd] = useState(() => getDefaultAuthFooterAdSettings())

  const [username, setUsername] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')

  useEffect(() => {
    getRegistrationStatus()
      .then((result) => {
        setRegistrationEnabled(result.enabled)
        if (!result.enabled) {
          addToast({ type: 'warning', message: '邀请码注册功能已关闭' })
          setTimeout(() => navigate('/login'), 1500)
        }
      })
      .catch(() => {})

    getAuthFooterAdSettings()
      .then((result) => setAuthFooterAd(result))
      .catch(() => {})
  }, [navigate, addToast])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !inviteCode.trim() || !password || !confirmPassword) {
      addToast({ type: 'error', message: '请填写用户名、邀请码和密码' })
      return
    }
    if (password !== confirmPassword) {
      addToast({ type: 'error', message: '两次输入的密码不一致' })
      return
    }
    if (password.length < 6) {
      addToast({ type: 'error', message: '密码长度至少6位' })
      return
    }
    setLoading(true)
    try {
      const result = await register({
        username: username.trim(),
        invite_code: inviteCode.trim(),
        password,
      })
      if (result.success) {
        addToast({ type: 'success', message: '注册申请已提交，请等待管理员审核' })
        navigate('/login')
      } else {
        addToast({ type: 'error', message: result.message || '注册失败' })
      }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: string; message?: string } } }
      const errorMsg = err?.response?.data?.detail || err?.response?.data?.message || '注册失败，请检查网络连接'
      addToast({ type: 'error', message: errorMsg })
    } finally {
      setLoading(false)
    }
  }

  if (!registrationEnabled) {
    return (
      <div className="min-h-screen bg-slate-50 dark:bg-slate-900 flex items-center justify-center p-4">
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow-sm border border-slate-200 dark:border-slate-700 p-8 text-center max-w-sm">
          <div className="w-14 h-14 rounded-full bg-amber-100 dark:bg-amber-900/30 mx-auto mb-4 flex items-center justify-center">
            <span className="text-2xl">🚫</span>
          </div>
          <h1 className="text-lg vben-card-title text-slate-900 dark:text-slate-100 mb-2">注册功能已关闭</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mb-6">管理员已关闭邀请码注册，如需账号请联系管理员</p>
          <Link to="/login" className="btn-ios-primary">返回登录</Link>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900 transition-colors">
      <AuthNavbar />
      <div className="pt-20 pb-10 px-4 sm:px-6 flex items-start justify-center min-h-screen">
        <div className="w-full max-w-md">
          <div className="text-center mb-6">
            <div className="w-12 h-12 rounded-xl bg-blue-600 text-white mx-auto mb-4 flex items-center justify-center">
              <MessageSquare className="w-6 h-6" />
            </div>
            <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">邀请码注册</h1>
            <p className="text-sm text-slate-500 dark:text-slate-400">使用管理员发放的邀请码创建账号</p>
          </div>

          <div className="bg-white dark:bg-slate-800 rounded-lg shadow-sm border border-slate-200 dark:border-slate-700 p-6">
            <div className="mb-4 rounded-lg bg-blue-50 dark:bg-blue-900/20 px-3 py-2.5 text-xs leading-5 text-blue-700 dark:text-blue-300">
              每个邀请码只能注册一次。输入时可忽略邀请码中的短横线和空格。
            </div>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="input-group">
                <label className="input-label">用户名</label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="请输入用户名" className="input-ios pl-9" />
                </div>
              </div>

              <div className="input-group">
                <label className="input-label">邀请码</label>
                <div className="relative">
                  <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <input type="text" value={inviteCode} onChange={(e) => setInviteCode(e.target.value.toUpperCase())} placeholder="请输入管理员提供的邀请码" className="input-ios pl-9 tracking-wide" autoComplete="one-time-code" />
                </div>
              </div>

              <div className="input-group">
                <label className="input-label">密码</label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <input type={showPassword ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="至少6位字符" className="input-ios pl-9 pr-9" />
                  <button type="button" onClick={() => setShowPassword(!showPassword)} className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300">
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <div className="input-group">
                <label className="input-label">确认密码</label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <input type={showPassword ? 'text' : 'password'} value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="请再次输入密码" className="input-ios pl-9" />
                </div>
              </div>

              <button type="submit" disabled={loading} className="w-full btn-ios-primary">
                {loading ? <ButtonLoading /> : '注 册'}
              </button>
            </form>

            <p className="text-center mt-6 text-slate-500 dark:text-slate-400 text-sm">
              已有账号？{' '}
              <Link to="/login" className="text-blue-600 dark:text-blue-400 font-medium hover:text-indigo-700">立即登录</Link>
            </p>
          </div>

          <SafeHtml html={authFooterAd['auth.footer_ad_html']} className="mt-6 text-center text-xs text-slate-400 dark:text-slate-500" />
        </div>
      </div>
    </div>
  )
}
