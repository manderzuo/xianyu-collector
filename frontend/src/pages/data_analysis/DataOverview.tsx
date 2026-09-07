/**
 * 数据总览页面
 *
 * 展示卖家数据概览，包括核心指标卡片和趋势图表
 * 支持多账号切换、时间范围选择和自定义日期范围
 */
import { useEffect, useState, useCallback, useMemo } from 'react'
import { motion } from 'framer-motion'
import {
  BarChart3,
  ShoppingCart,
  DollarSign,
  Users,
  TrendingUp,
  TrendingDown,
  RefreshCw,
  Package,
  Pause,
  Play,
} from 'lucide-react'
import { getAccountDetails } from '@/api/accounts'
import {
  getSellerSummary,
  type BannerDataItem,
  type GraphDataItem,
  type SellerSummaryRequest,
  getBrowseSummary,
  type BrowseSummaryData,
  type BrowseSummaryRequest,
} from '@/api/data_analysis'
import { useUIStore } from '@/store/uiStore'
import type { AccountDetail } from '@/types'
import { useLiveRefresh } from '@/utils/liveRefresh'
import { BrowseDistribution } from './BrowseDistribution'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts'

/** 时间范围选项 */
const DATE_TYPE_OPTIONS = [
  { value: 'recent1d', label: '近1天' },
  { value: 'recent7d', label: '近7天' },
  { value: 'recent30d', label: '近30天' },
  { value: 'customDate', label: '自定义' },
] as const

type DateTypeValue = typeof DATE_TYPE_OPTIONS[number]['value']

/** 指标名称映射（中文） */
const METRIC_NAME_MAP: Record<string, string> = {
  payAmt: '支付金额（元）',
  fstByrPayAmt: '首次买家支付金额',
  rptByrPayAmt: '复购买家支付金额',
  payOrdCnt: '支付笔数',
  aov: '客单价（元）',
  rfdAmt: '退款金额（元）',
  showUv: '商品曝光人数',
  showPv: '商品曝光次数',
  ipvUv: '商品浏览人数',
  ipv: '商品浏览次数',
  payByrCnt: '支付买家数',
  vstPv: '商品访问次数',
  vstUv: '商品访问人数',
  showItmCnt: '曝光商品数',
  ipvItmCnt: '访问商品数',
  stItmCnt: '成交商品数',
  uctr: '访问转化率',
  onlCnt: '在架商品数',
  chatUv: '咨询人数',
  rptOrdCnt: '复购订单数',
  rptByrCnt: '复购买家数',
  rpr: '复购率',
  rep3minUvRate: '3分钟回复率',
  showPvCmpPctl: '曝光竞争力',
  payOrdCntCmpPctl: '成交竞争力',
  rfdOrdCnt: '退款笔数',
  addRecItemCnt: '加入推荐商品数',
  priceCutItmCnt: '降价商品数',
  favCnt: '收藏数',
  newItmCnt: '新发商品数',
  cmtItmCnt: '评价商品数',
}

/** 客户展示版四项核心指标（轮播时随账号整体切换） */
const CORE_METRICS = [
  { name: 'payOrdCnt', icon: ShoppingCart, label: '支付笔数' },
  { name: 'payAmt', icon: DollarSign, label: '支付金额（元）' },
  { name: 'chatUv', icon: Users, label: '咨询人数' },
  { name: 'onlCnt', icon: Package, label: '在架商品数' },
]

const isAccountEnabled = (account: AccountDetail) => {
  const value = account.enabled as unknown
  return value === true || value === 1 || value === '1' || value === 'true'
}

export function DataOverview() {
  const { addToast } = useUIStore()
  const [accounts, setAccounts] = useState<AccountDetail[]>([])
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null)
  const [dateType, setDateType] = useState<DateTypeValue>('recent1d')
  const [customStartDate, setCustomStartDate] = useState('')
  const [customEndDate, setCustomEndDate] = useState('')
  const [loading, setLoading] = useState(false)
  const [bannerData, setBannerData] = useState<BannerDataItem[]>([])
  const [graphData, setGraphData] = useState<GraphDataItem[]>([])
  const [chartMetric, setChartMetric] = useState('payOrdCnt')
  const [browseData, setBrowseData] = useState<BrowseSummaryData | null>(null)
  const [browseLoading, setBrowseLoading] = useState(false)
  const [isCarouselPlaying, setIsCarouselPlaying] = useState(false)
  const [carouselIndex, setCarouselIndex] = useState(0)
  const [carouselMetricIndex, setCarouselMetricIndex] = useState(0)

  const orderedAccounts = useMemo(
    () => [...accounts].sort((a, b) => (isAccountEnabled(a) === isAccountEnabled(b) ? 0 : isAccountEnabled(a) ? -1 : 1)),
    [accounts],
  )
  const carouselAccounts = useMemo(() => {
    const enabledAccounts = orderedAccounts.filter(isAccountEnabled)
    return enabledAccounts.length > 0 ? enabledAccounts : orderedAccounts
  }, [orderedAccounts])
  const selectedAccount = orderedAccounts.find((account) => Number(account.pk) === selectedAccountId)

  /** 将 yyyy-MM-dd 转为 yyyyMMdd */
  const toCompactDate = (dateStr: string): string => {
    return dateStr.replace(/-/g, '')
  }

  /** 加载账号列表 */
  useEffect(() => {
    const loadAccounts = async () => {
      try {
        const data = await getAccountDetails()
        setAccounts(data)
        const firstAvailable = data.find(isAccountEnabled) ?? data[0]
        if (firstAvailable) {
          setSelectedAccountId(Number(firstAvailable.pk))
        }
      } catch {
        addToast({ type: 'error', message: '加载账号列表失败' })
      }
    }
    loadAccounts()
  }, [])

  /** 获取数据 */
  const fetchData = useCallback(async () => {
    if (!selectedAccountId) return

    // 自定义日期范围校验
    if (dateType === 'customDate') {
      if (!customStartDate || !customEndDate) {
        addToast({ type: 'error', message: '请选择开始日期和结束日期' })
        return
      }
      if (customStartDate > customEndDate) {
        addToast({ type: 'error', message: '开始日期不能晚于结束日期' })
        return
      }
    }

    setLoading(true)
    try {
      const params: SellerSummaryRequest = {
        account_id: selectedAccountId,
        date_type: dateType,
        date_range: dateType === 'customDate'
          ? `${toCompactDate(customStartDate)}|${toCompactDate(customEndDate)}`
          : '',
      }
      const result = await getSellerSummary(params)
      if (result.success && result.data) {
        const summaryData = result.data.data?.graphBannerBenchData
        if (summaryData) {
          setBannerData(summaryData.bannerDataList || [])
          setGraphData(summaryData.graphDataList || [])
        } else {
          setBannerData([])
          setGraphData([])
        }
      } else {
        addToast({ type: 'error', message: result.message || '获取数据失败' })
      }
    } catch {
      addToast({ type: 'error', message: '获取数据失败，请稍后重试' })
    } finally {
      setLoading(false)
    }
  }, [selectedAccountId, dateType, customStartDate, customEndDate, addToast])

  /** 获取与顶部筛选保持一致的流量分布数据 */
  const fetchBrowseData = useCallback(async () => {
    if (!selectedAccountId) return

    if (dateType === 'customDate') {
      if (!customStartDate || !customEndDate || customStartDate > customEndDate) return
    }

    setBrowseLoading(true)
    try {
      const params: BrowseSummaryRequest = {
        account_id: selectedAccountId,
        date_type: dateType,
        date_range: dateType === 'customDate'
          ? `${toCompactDate(customStartDate)}|${toCompactDate(customEndDate)}`
          : '',
      }
      const result = await getBrowseSummary(params)
      if (result.success && result.data) {
        setBrowseData(result.data.data || null)
      } else {
        addToast({ type: 'error', message: result.message || '获取流量分布失败' })
      }
    } catch {
      addToast({ type: 'error', message: '获取流量分布失败，请稍后重试' })
      setBrowseData(null)
    } finally {
      setBrowseLoading(false)
    }
  }, [selectedAccountId, dateType, customStartDate, customEndDate, addToast])

  /** 账号或时间范围变化时重新获取数据（非自定义日期时自动触发） */
  useEffect(() => {
    if (selectedAccountId && dateType !== 'customDate') {
      fetchData()
      fetchBrowseData()
    }
  }, [selectedAccountId, dateType, fetchData, fetchBrowseData])

  useEffect(() => {
    const index = carouselAccounts.findIndex((account) => Number(account.pk) === selectedAccountId)
    if (index >= 0) setCarouselIndex(index)
  }, [selectedAccountId, carouselAccounts])

  /** 展示模式依次轮播当前账号的四项指标，四项完成后再切换到下一个账号。 */
  useEffect(() => {
    if (!isCarouselPlaying || carouselAccounts.length < 2) return
    const timer = window.setInterval(() => {
      setCarouselMetricIndex((currentMetricIndex) => {
        const nextMetricIndex = (currentMetricIndex + 1) % CORE_METRICS.length
        setChartMetric(CORE_METRICS[nextMetricIndex].name)
        if (nextMetricIndex === 0) {
          setCarouselIndex((currentAccountIndex) => {
            const nextAccountIndex = (currentAccountIndex + 1) % carouselAccounts.length
            setSelectedAccountId(Number(carouselAccounts[nextAccountIndex].pk))
            return nextAccountIndex
          })
        }
        return nextMetricIndex
      })
    }, 8000)
    return () => window.clearInterval(timer)
  }, [isCarouselPlaying, carouselAccounts])

  const handleQuery = useCallback(() => {
    if (dateType === 'customDate') {
      if (!customStartDate || !customEndDate) {
        addToast({ type: 'error', message: '请选择开始日期和结束日期' })
        return
      }
      if (customStartDate > customEndDate) {
        addToast({ type: 'error', message: '开始日期不能晚于结束日期' })
        return
      }
    }
    fetchData()
    fetchBrowseData()
  }, [dateType, customStartDate, customEndDate, addToast, fetchData, fetchBrowseData])

  /** 根据name查找banner数据 */
  const getBannerItem = (name: string): BannerDataItem | undefined => {
    return bannerData.find((item) => item.name === name)
  }

  useLiveRefresh(
    () => {
      if (!selectedAccountId || (dateType === 'customDate' && (!customStartDate || !customEndDate || customStartDate > customEndDate))) return
      void fetchData()
      void fetchBrowseData()
    },
    {
      topics: ['analytics', 'items', 'all'],
      intervalMs: 30000,
      enabled: Boolean(selectedAccountId),
    },
  )

  /** 格式化日期（20260527 -> 05/27） */
  const formatDate = (ds: string): string => {
    if (!ds || ds.length !== 8) return ds
    return `${ds.slice(4, 6)}/${ds.slice(6, 8)}`
  }

  /** 渲染涨跌幅 */
  const renderRatio = (item: BannerDataItem | undefined) => {
    if (!item || !item.ratioFormat || item.ratioFormat === '-') {
      return <span className="text-gray-400 text-xs">--</span>
    }
    const ratio = item.ratio ?? 0
    const isUp = ratio > 0
    const isDown = ratio < 0
    return (
      <span className={`text-xs flex items-center gap-0.5 ${isUp ? 'text-green-500' : isDown ? 'text-red-500' : 'text-gray-400'}`}>
        {isUp && <TrendingUp className="w-3 h-3" />}
        {isDown && <TrendingDown className="w-3 h-3" />}
        {isUp ? '+' : ''}{item.ratioFormat}
      </span>
    )
  }

  return (
    <div className="space-y-5 rounded-[28px] bg-slate-50 p-1 dark:bg-slate-950/40">
      {/* 客户展示版页头 */}
      <div className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-slate-950 via-slate-900 to-blue-950 px-6 py-6 text-white shadow-xl sm:px-8">
        <div className="pointer-events-none absolute -right-20 -top-24 h-72 w-72 rounded-full bg-cyan-400/10 blur-3xl" />
        <div className="relative flex flex-col gap-5 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-[0.2em] text-cyan-300">
              <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_12px_rgba(52,211,153,0.9)]" />
              Business intelligence
            </p>
            <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">数据总览</h2>
            <p className="mt-2 max-w-xl text-sm text-slate-300">把经营表现、商品表现和客户画像汇总在一块大屏里，适合对外展示与日常复盘。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1.5 text-slate-200">
              {isCarouselPlaying
                ? `轮播展示中 · 账号 ${Math.min(carouselIndex + 1, carouselAccounts.length)} / ${carouselAccounts.length} · 指标 ${carouselMetricIndex + 1} / ${CORE_METRICS.length}`
                : '单账号查看'}
            </span>
            {selectedAccount && (
              <span className="rounded-full border border-cyan-300/20 bg-cyan-300/10 px-3 py-1.5 text-cyan-200">
                当前：{selectedAccount.note || selectedAccount.id || `账号${selectedAccount.pk}`}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 顶部统一筛选栏 */}
      <div className="flex flex-col gap-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-800 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-semibold text-slate-800 dark:text-slate-100">经营视图</p>
          <p className="mt-1 text-xs text-slate-400">下方流量分布自动跟随此处的账号与日期</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* 账号选择 */}
          <select
            className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-blue-400 focus:ring-2 focus:ring-blue-100 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
            value={selectedAccountId ?? ''}
            onChange={(e) => {
              setIsCarouselPlaying(false)
              setSelectedAccountId(Number(e.target.value))
            }}
          >
            <option value="" disabled>选择账号</option>
            {orderedAccounts.map((acc) => (
              <option key={acc.pk} value={acc.pk}>
                {acc.note || acc.id || `账号${acc.pk}`}{isAccountEnabled(acc) ? '' : '（已禁用）'}
              </option>
            ))}
          </select>

          {/* 时间范围选择 */}
          <div className="flex overflow-hidden rounded-xl border border-slate-200 dark:border-slate-600">
            {DATE_TYPE_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                className={`px-3 py-2 text-xs transition-colors sm:text-sm ${
                  dateType === opt.value
                    ? 'bg-blue-600 text-white'
                    : 'bg-white text-slate-600 hover:bg-slate-50 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600'
                }`}
                onClick={() => setDateType(opt.value)}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {/* 自定义日期范围选择器 */}
          {dateType === 'customDate' && (
            <div className="flex items-center gap-2">
              <input
                type="date"
                className="rounded-xl border border-slate-200 bg-slate-50 px-2 py-2 text-sm text-slate-700 outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200"
                value={customStartDate}
                onChange={(e) => setCustomStartDate(e.target.value)}
              />
              <span className="text-sm text-slate-400">至</span>
              <input
                type="date"
                className="rounded-xl border border-slate-200 bg-slate-50 px-2 py-2 text-sm text-slate-700 outline-none focus:border-blue-400 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200"
                value={customEndDate}
                onChange={(e) => setCustomEndDate(e.target.value)}
              />
              <button
                className="rounded-xl bg-blue-600 px-3 py-2 text-sm text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
                onClick={handleQuery}
                disabled={loading || !selectedAccountId || !customStartDate || !customEndDate}
              >
                查询
              </button>
            </div>
          )}

          {/* 刷新按钮 */}
          <button
            className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600 transition hover:border-blue-300 hover:text-blue-600 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200"
            onClick={handleQuery}
            disabled={loading || browseLoading || !selectedAccountId}
            title="刷新数据"
          >
            <RefreshCw className={`h-4 w-4 ${loading || browseLoading ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <button
            className={`flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition ${isCarouselPlaying ? 'bg-amber-400 text-slate-950 hover:bg-amber-300' : 'bg-slate-900 text-white hover:bg-slate-700 dark:bg-blue-600 dark:hover:bg-blue-500'}`}
            onClick={() => {
              if (carouselAccounts.length < 2) {
                addToast({ type: 'info', message: '至少需要两个账号才能开启轮播' })
                return
              }
              if (!isCarouselPlaying) {
                const currentIndex = carouselAccounts.findIndex((account) => Number(account.pk) === selectedAccountId)
                setCarouselIndex(currentIndex >= 0 ? currentIndex : 0)
                if (currentIndex < 0) setSelectedAccountId(Number(carouselAccounts[0].pk))
                setCarouselMetricIndex(0)
                setChartMetric(CORE_METRICS[0].name)
              }
              setIsCarouselPlaying((playing) => !playing)
            }}
            disabled={carouselAccounts.length === 0}
          >
            {isCarouselPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            {isCarouselPlaying ? '暂停轮播' : '轮播展示'}
          </button>
        </div>
      </div>

      {/* 首次加载提示；已有内容更新时保留旧内容，避免整块闪烁 */}
      {loading && bannerData.length === 0 && (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500"></div>
          <span className="ml-3 text-gray-500 dark:text-gray-400">加载中...</span>
        </div>
      )}

      {/* 核心指标卡片 + 趋势图表（左右布局） */}
      {bannerData.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex flex-col lg:flex-row gap-4"
        >
          {/* 左侧：指标卡片 */}
          <motion.div
            className="grid w-full auto-rows-min grid-cols-2 gap-3 lg:w-[420px] lg:flex-shrink-0"
          >
            <div className="col-span-2 flex items-center justify-between rounded-2xl border border-blue-200/80 bg-gradient-to-r from-blue-600 to-cyan-500 px-4 py-3 text-white shadow-sm dark:border-blue-500/30">
              <div>
                <p className="text-xs font-medium text-blue-100">账号经营表现</p>
                <p className="mt-0.5 truncate text-sm font-semibold">{selectedAccount?.note || selectedAccount?.id || `账号${selectedAccountId}`}</p>
              </div>
              <div className="text-right text-[10px] text-blue-100">
                {isCarouselPlaying ? '轮播切换中' : '当前账号'}
                <div className="mt-1 flex justify-end gap-1">
                  {carouselAccounts.map((account, index) => (
                    <span
                      key={account.pk}
                      className={`h-1.5 rounded-full transition-all ${index === carouselIndex ? 'w-4 bg-white' : 'w-1.5 bg-white/40'}`}
                    />
                  ))}
                </div>
              </div>
            </div>
            {CORE_METRICS.map((metric) => {
              const item = getBannerItem(metric.name)
              if (!item) return null
              const Icon = metric.icon
              const isSelected = chartMetric === metric.name
              return (
                <div
                  key={metric.name}
                  className={`rounded-2xl border bg-white p-4 shadow-sm transition-all duration-300 cursor-pointer dark:bg-slate-800 ${
                    isSelected
                      ? 'border-blue-500 ring-2 ring-blue-100 shadow-md scale-[1.02] dark:ring-blue-900'
                      : 'border-slate-200 dark:border-slate-700 hover:border-blue-300 hover:shadow-md dark:hover:border-blue-600'
                  }`}
                  onClick={() => {
                    setChartMetric(metric.name)
                    if (isCarouselPlaying) {
                      const metricIndex = CORE_METRICS.findIndex((item) => item.name === metric.name)
                      if (metricIndex >= 0) setCarouselMetricIndex(metricIndex)
                    }
                  }}
                >
                  <div className="flex items-center mb-1.5">
                    <span className={`flex items-center gap-1 text-xs ${isSelected ? 'font-medium text-blue-600 dark:text-blue-400' : 'text-slate-500 dark:text-slate-400'}`}>
                      <Icon className="w-3.5 h-3.5" />
                      {metric.label}
                    </span>
                  </div>
                  <div className="flex items-end justify-between">
                    <motion.span
                      key={`${selectedAccountId}-${metric.name}-${item.dataStr}`}
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.24, ease: 'easeOut' }}
                      className="text-2xl font-bold tracking-tight text-slate-900 dark:text-white"
                    >
                      {metric.name === 'payAmt' || metric.name === 'rfdAmt' ? `¥${item.dataStr}` : item.dataStr}
                    </motion.span>
                    <div className="text-right">
                      {renderRatio(item)}
                      {item.lastDataStr && item.lastDataStr !== '-' && (
                        <div className="mt-0.5 text-xs text-slate-400">
                          前{item.cycle?.replace('前', '') || ''} {item.lastDataStr}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </motion.div>

          {/* 右侧：趋势图表 */}
          {graphData.length > 0 && (
            <div className="min-w-0 flex-1 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
              <div className="mb-4 flex items-center justify-between">
                <h3 className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
                  <BarChart3 className="w-4 h-4" />
                  {CORE_METRICS.find((m) => m.name === chartMetric)?.label || METRIC_NAME_MAP[chartMetric] || chartMetric} - 趋势图
                </h3>
                <span className="rounded-full bg-blue-50 px-2.5 py-1 text-[10px] font-medium text-blue-600 dark:bg-blue-500/10 dark:text-blue-300">趋势</span>
              </div>
              <div className="h-[400px]">
                <ResponsiveContainer
                  width="100%"
                  height="100%"
                  minWidth={0}
                  minHeight={320}
                  initialDimension={{ width: 1, height: 320 }}
                >
                  <LineChart data={graphData.map((item) => ({ ...item, date: formatDate(item.ds) }))}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.35} />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 12 }}
                      stroke="#9ca3af"
                    />
                    <YAxis
                      tick={{ fontSize: 12 }}
                      stroke="#9ca3af"
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        border: '1px solid #e5e7eb',
                        borderRadius: '8px',
                        fontSize: '12px',
                      }}
                      labelFormatter={(label) => `日期: ${label}`}
                      formatter={(value) => [
                        typeof value === 'number' && value < 1 && value > 0
                          ? `${(value * 100).toFixed(2)}%`
                          : String(value ?? ''),
                        CORE_METRICS.find((m) => m.name === chartMetric)?.label || chartMetric,
                      ]}
                    />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey={chartMetric}
                      name={CORE_METRICS.find((m) => m.name === chartMetric)?.label || chartMetric}
                      stroke="#3b82f6"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      activeDot={{ r: 5 }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </motion.div>
      )}

      {/* 流量分布模块：复用顶部账号和时间筛选 */}
      <BrowseDistribution
        browseData={browseData}
        loading={browseLoading}
        selectedAccountId={selectedAccountId}
        onRefresh={fetchBrowseData}
      />

      {/* 无数据提示 */}
      {!loading && bannerData.length === 0 && selectedAccountId && (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <BarChart3 className="w-12 h-12 mb-3" />
          <p>暂无数据</p>
          <p className="text-sm mt-1">请确认账号Cookie有效且已开通卖家数据罗盘</p>
        </div>
      )}

      {/* 未选择账号提示 */}
      {!selectedAccountId && (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Users className="w-12 h-12 mb-3" />
          <p>请先选择一个账号</p>
        </div>
      )}
    </div>
  )
}
