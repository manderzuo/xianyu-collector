import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { motion } from 'framer-motion'
import { ArrowUpRight, BarChart3, Bot, Check, Eye, EyeOff, KeyRound, Lock, Mail, MessageSquare, Moon, ShieldCheck, ShoppingBag, Sun, User, UsersRound, Zap } from 'lucide-react'
import { SafeHtml } from '@/components/common/SafeHtml'
import { getDefaultAuthFooterAdSettings, getDefaultLoginBrandingSettings } from '@/api/settings'
import { login, register, verifyToken, getRegistrationStatus, generateCaptcha, verifyCaptcha, sendVerificationCode, getLoginCaptchaStatus, getLoginBrandingSettings, getAuthFooterAdSettings } from '@/api/auth'
import { useAuthStore } from '@/store/authStore'
import { useUIStore } from '@/store/uiStore'
import { cn } from '@/utils/cn'
import { ButtonLoading } from '@/components/common/Loading'
import { GeetestCaptcha, type GeetestResult } from '@/components/common/GeetestCaptcha'
import { POPUP_ANNOUNCEMENT_SHOWN_KEY } from '@/components/common/PopupAnnouncementModal'
import { getApiErrorMessage } from '@/utils/apiError'
import { initializeThemeMode, toggleThemeMode } from '@/utils/theme'
import '@/styles/login.css'

type LoginType = 'username' | 'email-password' | 'email-code'

export function Login() {
  const navigate = useNavigate()
  const { setAuth, isAuthenticated } = useAuthStore()
  const { addToast } = useUIStore()

  const [loginType, setLoginType] = useState<LoginType>('username')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [registrationEnabled, setRegistrationEnabled] = useState(true)
  const [loginCaptchaEnabled, setLoginCaptchaEnabled] = useState<boolean | null>(null)
  const [loginBranding, setLoginBranding] = useState(() => getDefaultLoginBrandingSettings())
  const [authFooterAd, setAuthFooterAd] = useState(() => getDefaultAuthFooterAdSettings())
  const [showRegister, setShowRegister] = useState(false)
  const [isDark, setIsDark] = useState(() => (
    typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
  ))

  useEffect(() => {
    document.body.classList.add('auth-login-body')
    setIsDark(initializeThemeMode() === 'dark')

    return () => {
      document.body.classList.remove('auth-login-body')
    }
  }, [])

  const handleThemeToggle = () => {
    setIsDark(toggleThemeMode() === 'dark')
  }

  // Form states
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [email, setEmail] = useState('')
  const [emailPassword, setEmailPassword] = useState('')
  const [emailForCode, setEmailForCode] = useState('')
  const [captchaCode, setCaptchaCode] = useState('')
  const [verificationCode, setVerificationCode] = useState('')
  const [registerInviteCode, setRegisterInviteCode] = useState('')
  const [registerConfirmPassword, setRegisterConfirmPassword] = useState('')

  // Captcha states
  const [captchaImage, setCaptchaImage] = useState('')
  const [sessionId] = useState(() => `session_${Math.random().toString(36).substr(2, 9)}_${Date.now()}`)
  const [captchaVerified, setCaptchaVerified] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const [verifying, setVerifying] = useState(false)

  // 极验滑动验证码状态
  const [geetestResult, setGeetestResult] = useState<GeetestResult | null>(null)
  const [geetestKey, setGeetestKey] = useState(0)

  // 重置滑动验证码（登录失败时使用）
  const resetGeetest = () => {
    setGeetestResult(null)
    setGeetestKey((k) => k + 1)
  }

  // Check if already logged in
  useEffect(() => {
    if (isAuthenticated) {
      navigate('/dashboard')
      return
    }

    const token = localStorage.getItem('auth_token')
    if (token) {
      verifyToken()
        .then((result) => {
          if (result.authenticated) {
            navigate('/dashboard')
          }
        })
        .catch(() => {
          localStorage.removeItem('auth_token')
        })
    }
  }, [isAuthenticated, navigate])

  // Load initial states
  useEffect(() => {
    getRegistrationStatus()
      .then((result) => setRegistrationEnabled(result.enabled))
      .catch(() => {})

    getLoginCaptchaStatus()
      .then((result) => setLoginCaptchaEnabled(result.enabled))
      .catch(() => {})

    getLoginBrandingSettings()
      .then((result) => setLoginBranding(result))
      .catch(() => {})

    getAuthFooterAdSettings()
      .then((result) => setAuthFooterAd(result))
      .catch(() => {})
  }, [])

  // Load captcha when switching to email-code
  useEffect(() => {
    if (loginType === 'email-code') {
      loadCaptcha()
    }
  }, [loginType])

  // Countdown timer
  useEffect(() => {
    if (countdown > 0) {
      const timer = setTimeout(() => setCountdown(countdown - 1), 1000)
      return () => clearTimeout(timer)
    }
  }, [countdown])

  // 自动验证图形验证码
  useEffect(() => {
    if (captchaCode.length === 4 && !captchaVerified && !verifying && loginType === 'email-code') {
      handleVerifyCaptchaAuto()
    }
  }, [captchaCode])

  const handleVerifyCaptchaAuto = async () => {
    if (captchaCode.length !== 4 || verifying) return
    setVerifying(true)
    try {
      const result = await verifyCaptcha(sessionId, captchaCode)
      if (result.success) {
        setCaptchaVerified(true)
        addToast({ type: 'success', message: '验证码验证成功' })
      } else {
        setCaptchaVerified(false)
        loadCaptcha()
        addToast({ type: 'error', message: '验证码错误' })
      }
    } catch {
      addToast({ type: 'error', message: '验证失败' })
    } finally {
      setVerifying(false)
    }
  }

  const loadCaptcha = async () => {
    try {
      const result = await generateCaptcha(sessionId)
      if (result.success && result.captcha_image) {
        setCaptchaImage(result.captcha_image)
        setCaptchaVerified(false)
        setCaptchaCode('')
      }
    } catch {
      addToast({ type: 'error', message: '加载验证码失败' })
    }
  }

  const handleSendCode = async () => {
    if (!captchaVerified || !emailForCode || countdown > 0) return

    try {
      const result = await sendVerificationCode(emailForCode, 'login', sessionId)
      if (result.success) {
        setCountdown(60)
        addToast({ type: 'success', message: '验证码已发送' })
      } else {
        addToast({ type: 'error', message: result.message || '发送失败' })
      }
    } catch {
      addToast({ type: 'error', message: '发送验证码失败' })
    }
  }

  // 切换登录类型时的处理
  const handleLoginTypeChange = (newType: LoginType) => {
    const oldType = loginType
    setLoginType(newType)
    
    // 只有在需要滑块验证的类型之间切换时，才重置验证结果
    // 用户名登录 <-> 邮箱密码登录：共用滑块，不需要重置
    // 切换到/从验证码登录：需要重置
    const needsGeetest = (type: LoginType) => type === 'username' || type === 'email-password'
    
    if (needsGeetest(oldType) !== needsGeetest(newType)) {
      // 从需要滑块切换到不需要，或反过来，重置状态
      setGeetestResult(null)
    } else if (needsGeetest(oldType) && needsGeetest(newType)) {
      // 在两个需要滑块的类型之间切换，保持验证结果
      // 不做任何操作
    }
  }

  // 极验验证成功回调
  const handleGeetestSuccess = (result: GeetestResult) => {
    setGeetestResult(result)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)

    try {
      let loginData: any = {}

      if (loginType === 'username') {
        if (!username || !password) {
          addToast({ type: 'error', message: '请输入用户名和密码' })
          setLoading(false)
          return
        }
        // 检查滑动验证码（仅确认开启时前端校验）
        if (loginCaptchaEnabled === true && !geetestResult) {
          addToast({ type: 'error', message: '请完成滑动验证' })
          setLoading(false)
          return
        }
        loginData = { 
          username, 
          password,
          geetest_challenge: geetestResult?.challenge,
          geetest_validate: geetestResult?.validate,
          geetest_seccode: geetestResult?.seccode
        }
      } else if (loginType === 'email-password') {
        if (!email || !emailPassword) {
          addToast({ type: 'error', message: '请输入邮箱和密码' })
          setLoading(false)
          return
        }
        // 检查滑动验证码（仅确认开启时前端校验）
        if (loginCaptchaEnabled === true && !geetestResult) {
          addToast({ type: 'error', message: '请完成滑动验证' })
          setLoading(false)
          return
        }
        loginData = { 
          email, 
          password: emailPassword,
          geetest_challenge: geetestResult?.challenge,
          geetest_validate: geetestResult?.validate,
          geetest_seccode: geetestResult?.seccode
        }
      } else {
        if (!emailForCode || !verificationCode) {
          addToast({ type: 'error', message: '请输入邮箱和验证码' })
          setLoading(false)
          return
        }
        loginData = { email: emailForCode, verification_code: verificationCode }
      }

      const result = await login(loginData)

      if (result.success && result.token && result.refresh_token) {
        // 清除弹窗公告会话标记，确保本次登录后重新弹窗展示一次
        sessionStorage.removeItem(POPUP_ANNOUNCEMENT_SHOWN_KEY)
        setAuth(result.token, result.refresh_token, {
          user_id: result.user_id!,
          username: result.username!,
          is_admin: result.is_admin!,
          account_limit: result.account_limit,
          role: result.role,
          plan_code: result.plan_code,
          entitlements: result.entitlements,
        })
        addToast({ type: 'success', message: '登录成功' })
        navigate('/dashboard')
      } else {
        addToast({ type: 'error', message: result.message || '登录失败' })
        // 登录失败，重置滑动验证
        resetGeetest()
      }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: string; message?: string } } }
      addToast({ type: 'error', message: err?.response?.data?.detail || err?.response?.data?.message || '登录失败，请检查网络连接' })
      // 登录失败，重置滑动验证
      resetGeetest()
    } finally {
      setLoading(false)
    }
  }

  const handleRegisterSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !registerInviteCode.trim() || !password || !registerConfirmPassword) {
      addToast({ type: 'error', message: '请填写用户名、邀请码和密码' })
      return
    }
    if (password !== registerConfirmPassword) {
      addToast({ type: 'error', message: '两次输入的密码不一致' })
      return
    }
    if (password.length < 6) {
      addToast({ type: 'error', message: '密码长度至少6位' })
      return
    }
    setLoading(true)
    try {
      const result = await register({ username: username.trim(), invite_code: registerInviteCode.trim(), password, session_id: sessionId })
      if (result.success) {
        addToast({ type: 'success', message: result.message || '注册申请已提交，请等待管理员审核' })
        setShowRegister(false)
        setRegisterInviteCode('')
        setRegisterConfirmPassword('')
        setPassword('')
        setCaptchaVerified(false)
      } else {
        addToast({ type: 'error', message: result.message || '注册失败' })
      }
    } catch (error: unknown) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '注册失败，请稍后重试') })
    } finally {
      setLoading(false)
    }
  }

  const displayTitleLines = loginBranding['login.system_title']
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  const titleLines = displayTitleLines.length > 0 ? displayTitleLines : ['多账号，', '统一管理']

  return (
    <div className="auth-login-page">
      <header className="auth-login-topbar">
        <Link to="/login" className="auth-login-brand" aria-label={loginBranding['login.system_name']}>
          <span className="auth-login-brand-logo" aria-hidden="true">
            <MessageSquare />
          </span>
          <span>{loginBranding['login.system_name']}</span>
        </Link>

        <div className="auth-login-top-actions">
          <span className="auth-login-mode-label">{isDark ? '深色模式' : '浅色模式'}</span>
          <button
            type="button"
            className="auth-login-theme-toggle"
            onClick={handleThemeToggle}
            aria-label={isDark ? '切换到浅色模式' : '切换到深色模式'}
            aria-pressed={isDark}
            title={isDark ? '切换到浅色模式' : '切换到深色模式'}
          >
            {isDark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
          </button>
        </div>
      </header>

      <main className="auth-login-layout">
        <motion.section
          className="auth-login-showcase"
          initial={{ opacity: 0, x: -24 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.55 }}
          aria-labelledby="auth-login-hero-title"
        >
          <div className="auth-login-showcase-label">专业 · 稳定 · 高效</div>
          <h1 id="auth-login-hero-title" className="auth-login-hero-title">
            {titleLines.map((line, index) => (
              <span
                key={`${line}-${index}`}
                className={cn(
                  'auth-login-hero-title-line',
                  index === titleLines.length - 1 && 'auth-login-hero-title-line--accent',
                )}
              >
                {line}
              </span>
            ))}
          </h1>
          <p className="auth-login-hero-description">{loginBranding['login.system_description']}</p>

          <div className="auth-login-feature-grid" aria-label="平台能力">
            <div className="auth-login-feature">
              <span className="auth-login-feature-icon"><UsersRound aria-hidden="true" /></span>
              <strong>多账号集中管理</strong>
              <span>高效切换，省时省力</span>
            </div>
            <div className="auth-login-feature">
              <span className="auth-login-feature-icon"><Bot aria-hidden="true" /></span>
              <strong>自动化运营</strong>
              <span>批量任务，稳定执行</span>
            </div>
            <div className="auth-login-feature">
              <span className="auth-login-feature-icon"><BarChart3 aria-hidden="true" /></span>
              <strong>数据分析</strong>
              <span>经营数据，一目了然</span>
            </div>
          </div>

          <div className="auth-login-visual" aria-hidden="true">
            <div className="auth-login-floating-card auth-login-floating-card--left">
              <ShoppingBag />
              <span>商品管理</span>
            </div>
            <div className="auth-login-dashboard">
              <div className="auth-login-dashboard-head">
                <span className="auth-login-dashboard-title"><i />运营数据</span>
                <span className="auth-login-dashboard-status"><Zap />实时同步</span>
              </div>
              <div className="auth-login-chart">
                <div className="auth-login-chart-bars">
                  <span /><span /><span /><span /><span /><span />
                </div>
                <svg viewBox="0 0 520 90" preserveAspectRatio="none">
                  <path d="M4 76 C40 69 54 75 78 61 S119 70 145 51 S182 56 210 44 S250 58 282 36 S322 48 348 28 S392 39 422 18 S474 28 516 5" />
                </svg>
              </div>
              <div className="auth-login-dashboard-stats">
                <div className="auth-login-dashboard-stat">
                  <ShoppingBag aria-hidden="true" />
                  <small>商品管理</small>
                  <strong>1,268</strong>
                </div>
                <div className="auth-login-dashboard-stat">
                  <MessageSquare aria-hidden="true" />
                  <small>自动回复</small>
                  <strong>98%</strong>
                </div>
                <div className="auth-login-dashboard-stat">
                  <BarChart3 aria-hidden="true" />
                  <small>经营分析</small>
                  <strong>实时</strong>
                </div>
              </div>
            </div>
            <div className="auth-login-floating-card auth-login-floating-card--right">
              <ShieldCheck />
              <span>安全稳定运行</span>
            </div>
          </div>
        </motion.section>

        <div className="auth-login-panel-wrap">
          <motion.section
            className="auth-login-panel"
            initial={{ opacity: 0, y: 22 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.45, delay: 0.08 }}
            aria-labelledby="auth-login-panel-title"
          >
            <div className="auth-login-panel-heading">
              <span className="auth-login-panel-eyebrow">{showRegister ? 'INVITATION REGISTRATION' : 'SECURE WORKSPACE'}</span>
              <h2 id="auth-login-panel-title">{showRegister ? '邀请码注册' : '登录'}</h2>
              <p>{showRegister ? '提交申请后由管理员审核，审核通过即可登录' : '欢迎回来，请登录您的账号'}</p>
            </div>

            {!showRegister && (
              <div className="auth-login-tab-list" role="tablist" aria-label="登录方式">
                {[
                  { type: 'username' as const, label: '账号登录' },
                  { type: 'email-password' as const, label: '邮箱密码' },
                  { type: 'email-code' as const, label: '验证码' },
                ].map((tab) => (
                  <button
                    key={tab.type}
                    type="button"
                    role="tab"
                    aria-selected={loginType === tab.type}
                    className={cn('auth-login-tab', loginType === tab.type && 'auth-login-tab--active')}
                    onClick={() => handleLoginTypeChange(tab.type)}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>
            )}

            {showRegister ? (
              <form onSubmit={handleRegisterSubmit} className="auth-login-form auth-login-register-form">
                <div className="auth-login-field">
                  <label htmlFor="register-username">用户名</label>
                  <div className="auth-login-input-shell">
                    <User aria-hidden="true" />
                    <input id="register-username" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="请输入用户名" className="auth-login-input" autoComplete="username" />
                  </div>
                </div>
                <div className="auth-login-field">
                  <label htmlFor="register-invite-code">邀请码</label>
                  <div className="auth-login-input-shell">
                    <KeyRound aria-hidden="true" />
                    <input id="register-invite-code" value={registerInviteCode} onChange={(e) => setRegisterInviteCode(e.target.value.toUpperCase())} placeholder="请输入管理员提供的邀请码" className="auth-login-input" autoComplete="one-time-code" />
                  </div>
                </div>
                <div className="auth-login-field">
                  <label htmlFor="register-password">密码</label>
                  <div className="auth-login-input-shell">
                    <Lock aria-hidden="true" />
                    <input id="register-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="至少6位字符" className="auth-login-input" autoComplete="new-password" />
                    <button type="button" onClick={() => setShowPassword(!showPassword)} className="auth-login-password-toggle" aria-label={showPassword ? '隐藏密码' : '显示密码'}>
                      {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                    </button>
                  </div>
                </div>
                <div className="auth-login-field">
                  <label htmlFor="register-confirm-password">确认密码</label>
                  <div className="auth-login-input-shell">
                    <Lock aria-hidden="true" />
                    <input id="register-confirm-password" type={showPassword ? 'text' : 'password'} value={registerConfirmPassword} onChange={(e) => setRegisterConfirmPassword(e.target.value)} placeholder="请再次输入密码" className="auth-login-input" autoComplete="new-password" />
                  </div>
                </div>
                <button type="submit" disabled={loading} className="auth-login-submit">
                  {loading ? <ButtonLoading /> : <>提交注册申请 <ArrowUpRight aria-hidden="true" /></>}
                </button>
              </form>
            ) : (
              <form onSubmit={handleSubmit} className="auth-login-form">
                {loginType === 'username' && (
                  <>
                    <div className="auth-login-field">
                      <label htmlFor="login-username">用户名</label>
                      <div className="auth-login-input-shell">
                        <User aria-hidden="true" />
                        <input id="login-username" type="text" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="请输入用户名" className="auth-login-input" autoComplete="username" />
                      </div>
                    </div>
                    <div className="auth-login-field">
                      <label htmlFor="login-password">密码</label>
                      <div className="auth-login-input-shell">
                        <Lock aria-hidden="true" />
                        <input id="login-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="请输入密码" className="auth-login-input" autoComplete="current-password" />
                        <button type="button" onClick={() => setShowPassword(!showPassword)} className="auth-login-password-toggle" aria-label={showPassword ? '隐藏密码' : '显示密码'}>
                          {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                        </button>
                      </div>
                    </div>
                  </>
                )}

                {loginType === 'email-password' && (
                  <>
                    <div className="auth-login-field">
                      <label htmlFor="login-email">邮箱地址</label>
                      <div className="auth-login-input-shell">
                        <Mail aria-hidden="true" />
                        <input id="login-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" className="auth-login-input" autoComplete="email" />
                      </div>
                    </div>
                    <div className="auth-login-field">
                      <label htmlFor="login-email-password">密码</label>
                      <div className="auth-login-input-shell">
                        <Lock aria-hidden="true" />
                        <input id="login-email-password" type={showPassword ? 'text' : 'password'} value={emailPassword} onChange={(e) => setEmailPassword(e.target.value)} placeholder="请输入密码" className="auth-login-input" autoComplete="current-password" />
                        <button type="button" onClick={() => setShowPassword(!showPassword)} className="auth-login-password-toggle" aria-label={showPassword ? '隐藏密码' : '显示密码'}>
                          {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                        </button>
                      </div>
                    </div>
                  </>
                )}

                {loginCaptchaEnabled === true && (loginType === 'username' || loginType === 'email-password') && (
                  <div className="auth-login-field">
                    <label>滑动验证</label>
                    <div className="auth-login-geetest">
                      <GeetestCaptcha
                        key={geetestKey}
                        onSuccess={handleGeetestSuccess}
                        onError={(err) => addToast({ type: 'error', message: err })}
                        disabled={loading}
                      />
                    </div>
                  </div>
                )}

                {loginType === 'email-code' && (
                  <>
                    <div className="auth-login-field">
                      <label htmlFor="login-code-email">邮箱地址</label>
                      <div className="auth-login-input-shell">
                        <Mail aria-hidden="true" />
                        <input id="login-code-email" type="email" value={emailForCode} onChange={(e) => setEmailForCode(e.target.value)} placeholder="name@example.com" className="auth-login-input" autoComplete="email" />
                      </div>
                    </div>
                    <div className="auth-login-field">
                      <label htmlFor="login-captcha-code">图形验证码</label>
                      <div className="auth-login-captcha-row">
                        <input id="login-captcha-code" type="text" value={captchaCode} onChange={(e) => setCaptchaCode(e.target.value)} placeholder="输入验证码" maxLength={4} className="auth-login-input" disabled={captchaVerified} autoComplete="one-time-code" />
                        {captchaImage ? <img src={captchaImage} alt="验证码，点击更换" onClick={loadCaptcha} className="auth-login-captcha-image" /> : <button type="button" className="auth-login-code-button" onClick={loadCaptcha}>获取验证码</button>}
                      </div>
                      <p className={cn('auth-login-status-hint', captchaVerified && 'auth-login-status-hint--success', verifying && 'auth-login-status-hint--checking')}>
                        {captchaVerified ? <><Check aria-hidden="true" /> 验证成功</> : verifying ? '验证中...' : '点击图片更换验证码'}
                      </p>
                    </div>
                    <div className="auth-login-field">
                      <label htmlFor="login-verification-code">邮箱验证码</label>
                      <div className="auth-login-code-row">
                        <div className="auth-login-input-shell">
                          <KeyRound aria-hidden="true" />
                          <input id="login-verification-code" type="text" value={verificationCode} onChange={(e) => setVerificationCode(e.target.value)} placeholder="6位数字验证码" maxLength={6} className="auth-login-input" autoComplete="one-time-code" />
                        </div>
                        <button type="button" onClick={handleSendCode} disabled={!captchaVerified || !emailForCode || countdown > 0} className="auth-login-code-button">
                          {countdown > 0 ? `${countdown}s` : '发送'}
                        </button>
                      </div>
                    </div>
                  </>
                )}

                <button type="submit" disabled={loading} className="auth-login-submit">
                  {loading ? <ButtonLoading /> : <>登录 <ArrowUpRight aria-hidden="true" /></>}
                </button>
              </form>
            )}

            <div className="auth-login-meta">
              <Link to="/forgot-password" className="auth-login-link">忘记密码？</Link>
              {registrationEnabled && (
                <button type="button" onClick={() => { setShowRegister((value) => !value); setCaptchaVerified(false); setCaptchaCode('') }} className="auth-login-link auth-login-link--primary">
                  {showRegister ? '返回登录' : <>立即注册 <ArrowUpRight aria-hidden="true" /></>}
                </button>
              )}
            </div>

            <SafeHtml html={authFooterAd['auth.footer_ad_html']} className="auth-login-footer" />
          </motion.section>
        </div>
      </main>
    </div>
  )
}
