import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get, post, request } from '../api/client'
import QrLoginPage from './QrLoginPage'

type Row = Record<string, unknown>
type FieldType = 'text' | 'number' | 'textarea' | 'select' | 'json'
type Field = { key: string; label: string; type?: FieldType; placeholder?: string; required?: boolean; defaultValue?: string; options?: Array<{ value: string; label: string }> }
type Props = { title: string; description: string; endpoint: string; icon: string }
type AccountDetail = {
  account?: Row
  login_state?: { has_cookie?: boolean; cookie_length?: number; cookie_records?: number; last_cookie_at?: string | null; expires_at?: string | null }
  connection?: { status?: string; cookie_loaded?: boolean; action?: string; updated_at?: string }
  content?: Record<string, number>
  sync?: { status?: string; last_started_at?: string | null; last_finished_at?: string | null; last_error?: string | null; products_count?: number; orders_count?: number; messages_count?: number }
  recent_products?: Array<{ id?: number; external_id?: string; title?: string; price?: number | null; status?: string; synced_at?: string | null }>
}

const accountContentLabels: Record<string, string> = {
  products: '商品',
  orders: '订单',
  messages: '消息',
  publish_jobs: '发布任务',
  publish_logs: '发布日志',
  keyword_rules: '关键词规则',
  default_replies: '默认回复',
}

function formatDetailDate(value?: string | null) {
  if (!value) return '暂无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const statusOptions = [
  { value: 'active', label: '启用' },
  { value: 'inactive', label: '停用' },
  { value: 'pending', label: '待处理' },
  { value: 'draft', label: '草稿' },
]

const fieldMap: Record<string, Field[]> = {
  '/items': [{ key: 'account_id', label: '账号 ID', type: 'number' }, { key: 'title', label: '商品标题', required: true }, { key: 'description', label: '商品描述', type: 'textarea' }, { key: 'price', label: '价格', type: 'number' }, { key: 'stock', label: '库存', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/cards': [{ key: 'name', label: '卡券名称', required: true }, { key: 'code', label: '卡券编码' }, { key: 'value', label: '卡券面值', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/message-filters': [{ key: 'keyword', label: '过滤关键词', required: true }, { key: 'action', label: '处理动作', defaultValue: 'block' }, { key: 'remark', label: '备注', type: 'textarea' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/blacklist/personal': [{ key: 'target_id', label: '用户或会话 ID', required: true }, { key: 'reason', label: '加入原因', type: 'textarea' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/notification-channels': [{ key: 'name', label: '渠道名称', required: true }, { key: 'channel', label: '渠道类型', defaultValue: 'webhook' }, { key: 'url', label: 'Webhook 地址' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/message-notifications': [{ key: 'title', label: '通知标题', required: true }, { key: 'content', label: '通知内容', type: 'textarea' }, { key: 'channel', label: '渠道', defaultValue: 'in_app' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/product-monitor/categories': [{ key: 'name', label: '分类名称', required: true }, { key: 'keyword', label: '搜索关键词' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/product-monitor/logs': [{ key: 'job_id', label: '任务 ID', type: 'number' }, { key: 'action', label: '执行动作', defaultValue: 'run' }, { key: 'detail', label: '执行说明', type: 'textarea' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/personal-settings': [{ key: 'key', label: '偏好键', required: true }, { key: 'value', label: '偏好值', type: 'textarea' }],
  '/cookies': [{ key: 'account_id', label: '账号 ID', type: 'number' }, { key: 'cookie_value', label: 'Cookie', type: 'textarea', required: true }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/qr': [{ key: 'account_id', label: '账号 ID', type: 'number', placeholder: '可留空，稍后绑定账号' }],
  '/proxy': [{ key: 'name', label: '代理名称', required: true }, { key: 'url', label: '代理地址', required: true, placeholder: 'http://用户名:密码@地址:端口' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/chat': [{ key: 'account_id', label: '账号 ID', type: 'number', required: true }, { key: 'peer_id', label: '对方账号 ID', required: true }, { key: 'content', label: '消息内容', type: 'textarea', required: true }, { key: 'msg_type', label: '消息类型', type: 'select', options: [{ value: 'text', label: '文字' }, { value: 'image', label: '图片' }] }],
  '/messages': [{ key: 'account_id', label: '账号 ID', type: 'number', required: true }, { key: 'peer_id', label: '对方账号 ID', required: true }, { key: 'direction', label: '方向', type: 'select', options: [{ value: 'in', label: '收到' }, { value: 'out', label: '发出' }] }, { key: 'content', label: '消息内容', type: 'textarea', required: true }],
  '/keywords': [{ key: 'keyword', label: '关键词', required: true }, { key: 'reply', label: '自动回复内容', type: 'textarea', required: true }, { key: 'match_mode', label: '匹配方式', type: 'select', options: [{ value: 'contains', label: '包含' }, { value: 'exact', label: '完全匹配' }] }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/default-replies': [{ key: 'title', label: '话术名称', required: true }, { key: 'content', label: '话术内容', type: 'textarea', required: true }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/ai': [{ key: 'provider', label: '服务商', required: true, placeholder: '例如：openai / custom' }, { key: 'model', label: '模型名称', required: true }, { key: 'base_url', label: '接口地址' }, { key: 'api_key', label: 'API Key', type: 'text' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/materials': [{ key: 'name', label: '素材名称', required: true }, { key: 'material_type', label: '素材类型', type: 'select', options: [{ value: 'image', label: '图片' }, { value: 'text', label: '文案' }, { value: 'video', label: '视频' }] }, { key: 'url', label: '素材地址', required: true }, { key: 'metadata_json', label: '附加信息', type: 'json' }],
  '/publish': [{ key: 'title', label: '商品标题', required: true }, { key: 'description', label: '商品描述', type: 'textarea' }, { key: 'price', label: '价格', type: 'number', required: true }, { key: 'account_id', label: '发布账号 ID', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: [{ value: 'draft', label: '草稿' }, { value: 'queued', label: '排队中' }] }],
  '/publish-addresses': [{ key: 'name', label: '地址名称', required: true }, { key: 'province', label: '省份' }, { key: 'city', label: '城市' }, { key: 'detail', label: '详细地址', required: true }],
  '/publish-capability': [{ key: 'account_id', label: '账号 ID', type: 'number' }, { key: 'capability', label: '能力名称', defaultValue: 'publish', required: true }],
  '/orders': [{ key: 'account_id', label: '账号 ID', type: 'number', required: true }, { key: 'order_no', label: '订单号' }, { key: 'buyer_id', label: '买家 ID' }, { key: 'product_id', label: '商品 ID', type: 'number' }, { key: 'amount', label: '订单金额', type: 'number', required: true }, { key: 'status', label: '状态', type: 'select', options: [{ value: 'pending', label: '待处理' }, { value: 'paid', label: '已付款' }, { value: 'shipped', label: '已发货' }, { value: 'completed', label: '已完成' }, { value: 'cancelled', label: '已取消' }] }],
  '/auto-rate': [{ key: 'name', label: '规则名称', required: true }, { key: 'content', label: '评价内容', type: 'textarea', required: true }, { key: 'delay_hours', label: '延迟小时', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/refund-cancel': [{ key: 'order_id', label: '订单 ID', type: 'number' }, { key: 'reason', label: '申请原因', required: true }, { key: 'status', label: '状态', type: 'select', options: [{ value: 'open', label: '待处理' }, { value: 'approved', label: '已同意' }, { value: 'rejected', label: '已拒绝' }] }, { key: 'resolution', label: '处理说明', type: 'textarea' }],
  '/goofish': [{ key: 'source', label: '来源', defaultValue: 'manual', required: true }, { key: 'external_id', label: '平台商品 ID', required: true }, { key: 'title', label: '商品标题', required: true }, { key: 'price', label: '价格', type: 'number' }, { key: 'payload', label: '原始数据', type: 'json' }],
  '/listing-monitor': [{ key: 'name', label: '监控名称', required: true }, { key: 'query', label: '搜索条件', required: true }, { key: 'interval_minutes', label: '间隔分钟', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/distribution': [{ key: 'source_id', label: '货源 ID', required: true }, { key: 'title', label: '商品标题', required: true }, { key: 'price', label: '价格', type: 'number' }, { key: 'status', label: '状态', type: 'select', options: [{ value: 'ready', label: '待分发' }, { value: 'synced', label: '已同步' }, { value: 'failed', label: '失败' }] }],
  '/compass': [{ key: 'metric_name', label: '指标名称', required: true }, { key: 'metric_value', label: '指标值', type: 'number', required: true }, { key: 'period', label: '统计周期', defaultValue: 'today', required: true }],
  '/notifications': [{ key: 'channel', label: '通知渠道', type: 'select', options: [{ value: 'in_app', label: '站内信' }, { value: 'email', label: '邮件' }, { value: 'webhook', label: 'Webhook' }] }, { key: 'title', label: '通知标题', required: true }, { key: 'content', label: '通知内容', type: 'textarea' }],
  '/risk-logs': [{ key: 'account_id', label: '账号 ID', type: 'number' }, { key: 'action', label: '风险动作', required: true }, { key: 'level', label: '等级', type: 'select', options: [{ value: 'info', label: '提示' }, { value: 'warn', label: '警告' }, { value: 'high', label: '高风险' }] }, { key: 'message', label: '说明', type: 'textarea' }],
  '/announcements': [{ key: 'title', label: '公告标题', required: true }, { key: 'content', label: '公告内容', type: 'textarea', required: true }, { key: 'level', label: '级别', type: 'select', options: [{ value: 'info', label: '普通' }, { value: 'warn', label: '重要' }] }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/popup': [{ key: 'title', label: '弹窗标题', required: true }, { key: 'content', label: '弹窗内容', type: 'textarea', required: true }, { key: 'level', label: '级别', type: 'select', options: [{ value: 'info', label: '普通' }, { value: 'warn', label: '重要' }] }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/feedback': [{ key: 'category', label: '反馈分类', defaultValue: 'general' }, { key: 'content', label: '反馈内容', type: 'textarea', required: true }],
  '/external': [{ key: 'provider', label: '服务名称', required: true }, { key: 'config', label: '连接配置', type: 'json' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/shared-scan': [],
  '/face-verification': [{ key: 'account_id', label: '账号 ID', type: 'number' }],
  '/payment': [{ key: 'order_id', label: '订单 ID', type: 'number' }, { key: 'provider', label: '支付渠道', defaultValue: 'manual', required: true }, { key: 'amount', label: '金额', type: 'number', required: true }],
  '/users': [{ key: 'username', label: '用户名', required: true }, { key: 'password', label: '初始密码', required: true }, { key: 'nickname', label: '昵称' }, { key: 'email', label: '邮箱' }, { key: 'role', label: '角色', type: 'select', options: [{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }] }, { key: 'status', label: '状态', type: 'select', options: statusOptions }],
  '/system-settings': [{ key: 'setting_key', label: '配置键', required: true }, { key: 'setting_value', label: '配置值', type: 'textarea' }, { key: 'is_secret', label: '敏感配置', type: 'select', options: [{ value: '0', label: '否' }, { value: '1', label: '是' }] }],
}

const actionMap: Record<string, { path: string; text: string }> = {
  '/chat': { path: '/chat/send', text: '发送消息' },
  '/qr': { path: '/qr/generate', text: '生成扫码会话' },
  '/face-verification': { path: '/face-verification/start', text: '发起核验' },
  '/shared-scan': { path: '/shared-scan/create', text: '创建扫码会话' },
  '/publish-capability': { path: '/publish-capability/check', text: '检查发布能力' },
  '/notifications': { path: '/notifications/send', text: '发送通知' },
}

function displayValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function initialForm(fields: Field[]) {
  return Object.fromEntries(fields.map((field) => [field.key, field.defaultValue || (field.type === 'select' ? field.options?.[0]?.value || '' : '')]))
}

function rowToForm(row: Row, fields: Field[]) {
  return Object.fromEntries(fields.map((field) => [field.key, row[field.key] === undefined || row[field.key] === null ? '' : typeof row[field.key] === 'object' ? JSON.stringify(row[field.key]) : String(row[field.key])]))
}

function ResourceTablePage({ title, description, endpoint, icon }: Props) {
  const navigate = useNavigate()
  const isAccounts = endpoint === '/accounts'
  const isItems = endpoint === '/items'
  const fields = useMemo(() => isAccounts ? [
    { key: 'account_name', label: '账号名称', required: true, placeholder: '例如：主账号' },
    { key: 'goofish_id', label: '平台账号 ID', placeholder: '可后续通过扫码补全' },
    { key: 'proxy', label: '代理地址', placeholder: 'http://用户名:密码@地址:端口' },
  ] : fieldMap[endpoint] || [{ key: 'title', label: '名称 / 标题' }, { key: 'content', label: '内容 / 备注', type: 'textarea' }, { key: 'status', label: '状态', type: 'select', options: statusOptions }], [endpoint, isAccounts])
  const action = actionMap[endpoint]
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [accountDetail, setAccountDetail] = useState<AccountDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [syncLoading, setSyncLoading] = useState(false)
  const [syncError, setSyncError] = useState('')

  async function load() {
    setError('')
    try {
      const result = await get<{ items?: Row[]; total?: number }>(endpoint)
      setRows(result.data?.items || []); setTotal(result.data?.total || 0)
    } catch (err) { setError(err instanceof Error ? err.message : '加载失败') }
  }

  async function loadAccountDetail(accountId: number) {
    const result = await get<AccountDetail>(`/accounts/${accountId}/detail`)
    setAccountDetail(result.data || {})
  }

  async function openAccountDetail(row: Row) {
    if (!isAccounts || !row.id) return
    setDetailLoading(true)
    setDetailError('')
    setSyncError('')
    setAccountDetail(null)
    try {
      await loadAccountDetail(Number(row.id))
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : '账号详情加载失败')
    } finally {
      setDetailLoading(false)
    }
  }

  async function syncAccountContent() {
    const accountId = Number(accountDetail?.account?.id)
    if (!accountId || syncLoading) return
    setSyncLoading(true)
    setSyncError('')
    try {
      const result = await post<{ message?: string }>(`/accounts/${accountId}/sync`, { page_size: 20, max_pages: 100 })
      await loadAccountDetail(accountId)
      setNotice(result.message || '账号内容同步完成')
    } catch (err) {
      setSyncError(err instanceof Error ? err.message : '账号内容同步失败')
      await loadAccountDetail(accountId).catch(() => undefined)
    } finally {
      setSyncLoading(false)
    }
  }
  useEffect(() => { void load() }, [endpoint])

  function openCreate() { setNotice(''); setError(''); setEditingId(null); setForm(initialForm(fields)); setShowForm(true) }
  function openEdit(row: Row) { setNotice(''); setError(''); setEditingId(Number(row.id)); setForm(rowToForm(row, fields)); setShowForm(true) }

  function makePayload() {
    const payload: Record<string, unknown> = {}
    for (const field of fields) {
      const value = form[field.key]
      if (field.type === 'number') payload[field.key] = value === '' ? null : Number(value)
      else if (field.type === 'json') {
        if (value) {
          try { payload[field.key] = JSON.parse(value) } catch { throw new Error(`${field.label}必须是有效 JSON`) }
        }
      } else if (value !== '') payload[field.key] = value
    }
    return payload
  }

  async function save() {
    for (const field of fields) if (field.required && !form[field.key]?.trim() && !(editingId && field.key === 'password')) { setError(`请填写${field.label}`); return }
    setSaving(true); setError('')
    try {
      const payload = makePayload()
      const target = editingId ? `/${endpoint.slice(1)}/${editingId}` : action?.path || endpoint
      const result = editingId ? await request(target, { method: 'PUT', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } }) : await post(target, payload)
      setNotice(result.message); setShowForm(false); await load()
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败') }
    finally { setSaving(false) }
  }

  async function remove(row: Row) {
    const id = row.id
    if (!id || !window.confirm(`确定删除这条${title}记录吗？`)) return
    try { const result = await request(`/${endpoint.slice(1)}/${id}`, { method: 'DELETE' }); setNotice(result.message); await load() }
    catch (err) { setError(err instanceof Error ? err.message : '删除失败') }
  }

  const columns = useMemo(() => rows.length ? Object.keys(rows[0]).filter((key) => !['id', 'owner_id', 'user_id', 'updated_at', 'created_at'].includes(key)).slice(0, 5) : [], [rows])
  const createText = action?.text || (isAccounts ? '添加账号' : isItems ? '新建商品' : `新建${title}`)
  const formTitle = editingId ? `编辑${title}` : createText

  return <div>
    <div className="page-heading"><div><p className="eyebrow">MODULE / {endpoint.replace('/', '').toUpperCase()}</p><h1><span className="title-icon">{icon}</span>{title}</h1><p className="muted">{description}</p></div><div className="heading-actions"><button className="secondary" onClick={() => void load()}>刷新数据</button><button className="primary" onClick={openCreate}>＋ {createText}</button></div></div>
    {notice && <div className="toast">{notice}</div>}
    <section className="panel resource-panel"><div className="toolbar"><div><h2>{isAccounts ? '账号列表' : isItems ? '账号同步商品' : `${title}记录`}</h2><p className="muted">数据来自当前登录用户的工作区。</p></div><span className="count-badge">{total} 条记录</span></div>
      {error && <div className="inline-error">{error}</div>}
      {rows.length ? <div className="data-table"><div className="data-row data-header"><span>ID</span>{columns.map((column) => <span key={column}>{column}</span>)}<span>操作</span></div>{rows.map((row, index) => <div className="data-row" key={String(row.id ?? index)}><span className="mono">{displayValue(row.id)}</span>{columns.map((column) => <span key={column} title={displayValue(row[column])}>{displayValue(row[column])}</span>)}<span className="row-actions">{isAccounts && <button className="text-button" onClick={() => void openAccountDetail(row)}>详情</button>}<button className="text-button" onClick={() => openEdit(row)}>编辑</button><button className="text-button danger" onClick={() => void remove(row)}>删除</button></span></div>)}</div> : <div className="empty-state"><div className="empty-symbol">{icon}</div><strong>这里还没有数据</strong><p>创建第一条记录后，系统会把结果展示在这里。</p><button className="secondary" onClick={openCreate}>{createText}</button></div>}
    </section>
    {showForm && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowForm(false) }}><div className="modal-card"><div className="modal-head"><div><p className="eyebrow">{isAccounts ? 'ACCOUNT' : 'RESOURCE'} / {editingId ? 'EDIT' : 'NEW'}</p><h2>{formTitle}</h2></div><button className="modal-close" onClick={() => setShowForm(false)}>×</button></div>{fields.map((field) => <label key={field.key}>{field.label}{!field.required && <span className="optional">可选</span>}{field.type === 'textarea' || field.type === 'json' ? <textarea autoFocus={!editingId && field === fields[0]} value={form[field.key] || ''} onChange={(e) => setForm({ ...form, [field.key]: e.target.value })} placeholder={field.placeholder || (field.type === 'json' ? '填写 JSON' : `填写${field.label}`)} /> : field.type === 'select' ? <select value={form[field.key] || ''} onChange={(e) => setForm({ ...form, [field.key]: e.target.value })}>{field.options?.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select> : <input autoFocus={!editingId && field === fields[0]} type={field.type === 'number' ? 'number' : 'text'} value={form[field.key] || ''} onChange={(e) => setForm({ ...form, [field.key]: e.target.value })} placeholder={field.placeholder || `填写${field.label}`} />}</label>)}<div className="modal-actions"><button className="secondary" onClick={() => setShowForm(false)}>取消</button><button className="primary" disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : editingId ? '保存修改' : '保存'}</button></div></div></div>}
    {isAccounts && detailLoading && <div className="modal-backdrop"><div className="modal-card account-detail-loading">正在加载账号详情…</div></div>}
    {isAccounts && detailError && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetailError('') }}><div className="modal-card"><div className="modal-head"><h2>账号详情加载失败</h2><button className="modal-close" onClick={() => setDetailError('')}>×</button></div><div className="inline-error">{detailError}</div><div className="modal-actions"><button className="secondary" onClick={() => setDetailError('')}>关闭</button></div></div></div>}
    {isAccounts && accountDetail && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setAccountDetail(null) }}><div className="modal-card account-detail-modal"><div className="modal-head"><div><p className="eyebrow">ACCOUNT / DETAIL</p><h2>{displayValue(accountDetail.account?.account_name || '闲鱼账号')}</h2></div><div className="modal-head-actions"><button className="primary" disabled={syncLoading || !accountDetail.login_state?.has_cookie} onClick={() => void syncAccountContent()}>{syncLoading ? '同步中…' : '立即同步'}</button><button className="modal-close" onClick={() => setAccountDetail(null)}>×</button></div></div>{syncError && <div className="inline-error">{syncError}</div>}<div className="account-detail-grid"><div><span>平台账号 ID</span><strong>{displayValue(accountDetail.account?.goofish_id)}</strong></div><div><span>账号状态</span><strong>{displayValue(accountDetail.account?.status)}</strong></div><div><span>连接状态</span><strong>{displayValue(accountDetail.connection?.status || '未加载')}</strong></div><div><span>Cookie 登录态</span><strong>{accountDetail.login_state?.has_cookie ? '已接入' : '未接入'}</strong></div><div><span>Cookie 记录</span><strong>{accountDetail.login_state?.cookie_records ?? 0} 条</strong></div><div><span>Cookie 长度</span><strong>{accountDetail.login_state?.cookie_length ?? 0} 字符</strong></div><div><span>最近保存</span><strong>{formatDetailDate(accountDetail.login_state?.last_cookie_at)}</strong></div><div><span>有效期</span><strong>{formatDetailDate(accountDetail.login_state?.expires_at)}</strong></div><div><span>最近同步</span><strong>{formatDetailDate(accountDetail.sync?.last_finished_at)}</strong></div><div><span>同步状态</span><strong>{displayValue(accountDetail.sync?.status || '未同步')}</strong></div></div><div className="account-detail-section"><div className="account-detail-section-head"><div><h3>账号内容</h3><p>商品会从闲鱼账号的“在售”列表同步；订单和消息需单独接入交易与 IM 接口。</p></div></div><div className="account-content-grid">{Object.entries(accountContentLabels).map(([key, label]) => <button key={key} className="account-content-card" onClick={() => { setAccountDetail(null); navigate(`/${key === 'products' ? 'items' : key === 'keyword_rules' ? 'keywords' : key === 'default_replies' ? 'default-replies' : key === 'publish_jobs' || key === 'publish_logs' ? 'publish' : key}`) }}><span>{label}</span><strong>{accountDetail.content?.[key] ?? 0}</strong><small>进入查看</small></button>)}</div></div><div className="account-detail-section"><div className="account-detail-section-head"><div><h3>最近同步商品</h3><p>平台商品 ID 和本地同步时间可用于核对数据。</p></div></div>{accountDetail.recent_products?.length ? <div className="account-recent-products">{accountDetail.recent_products.map((item) => <div className="account-recent-product" key={String(item.id)}><div><strong>{displayValue(item.title)}</strong><small>ID {displayValue(item.external_id)}</small></div><span>{item.price === null || item.price === undefined ? '—' : `¥${item.price}`}</span><em>{displayValue(item.status)}</em></div>)}</div> : <div className="account-recent-empty">尚未同步到商品，点击右上角“立即同步”。</div>}</div><div className="modal-actions"><button className="secondary" onClick={() => setAccountDetail(null)}>关闭</button></div></div></div>}
  </div>
}

export default function ResourcePage(props: Props) {
  return props.endpoint === '/qr' ? <QrLoginPage /> : <ResourceTablePage {...props} />
}
