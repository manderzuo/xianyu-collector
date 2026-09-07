/**
 * 流量分布组件
 *
 * 账号和时间范围由数据总览顶部统一控制，这里只负责展示结果，避免重复选择。
 */
import { motion } from 'framer-motion'
import { MapPin, MessageCircle, Package, Radio } from 'lucide-react'
import type { BrowseSummaryData, ProfileItem } from '@/api/data_analysis'

/** 单个分布卡片 */
function DistributionCard({
  title,
  caption,
  items,
  labelWidth = 'w-20',
  icon: Icon,
  emptyMessage,
}: {
  title: string
  caption: string
  items: ProfileItem[]
  labelWidth?: string
  icon: typeof Radio
  emptyMessage: string
}) {
  return (
    <div className="min-h-[292px] rounded-2xl border border-slate-200/80 bg-white p-5 shadow-sm transition-shadow hover:shadow-lg dark:border-slate-700 dark:bg-slate-800/90">
      <div className="mb-5 flex items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-300">
              <Icon className="h-4 w-4" />
            </span>
            {title}
          </h3>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{caption}</p>
        </div>
        <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] text-slate-500 dark:bg-slate-700 dark:text-slate-400">
          本周期
        </span>
      </div>
      <div className="h-[206px] space-y-3 overflow-y-auto pr-1">
        {items.map((item, idx) => (
          <div key={`${item.profileCode}-${idx}`} className="flex items-center gap-2.5">
            <span
              className={`truncate text-xs text-slate-600 dark:text-slate-300 ${labelWidth} flex-shrink-0`}
              title={item.profileVal}
            >
              {item.profileVal}
            </span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
              <div
                className="h-full rounded-full bg-gradient-to-r from-blue-500 to-cyan-400 transition-all"
                style={{ width: `${Math.min(Math.max(item.usrRatio, 0), 100)}%` }}
              />
            </div>
            <span className="w-14 flex-shrink-0 text-right text-xs font-medium text-slate-500 dark:text-slate-400">
              {item.usrRatioFormat}
            </span>
          </div>
        ))}
        {items.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 text-center dark:border-slate-700">
            <Icon className="mb-2 h-6 w-6 text-slate-300 dark:text-slate-600" />
            <p className="text-xs text-slate-400 dark:text-slate-500">{emptyMessage}</p>
          </div>
        )}
      </div>
    </div>
  )
}

export interface BrowseDistributionProps {
  browseData: BrowseSummaryData | null
  loading: boolean
  selectedAccountId: number | null
  onRefresh: () => void
}

export function BrowseDistribution({
  browseData,
  loading,
  selectedAccountId,
  onRefresh,
}: BrowseDistributionProps) {
  if (!selectedAccountId) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-300 py-14 text-center text-sm text-slate-400 dark:border-slate-700">
        请先在上方选择账号
      </div>
    )
  }

  return (
    <section className="space-y-4">
      <div className="flex items-end justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-blue-500">Audience insights</p>
          <h2 className="mt-1 text-xl font-bold text-slate-900 dark:text-white">流量分布</h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">已跟随上方账号与日期范围自动更新，无需二次选择</p>
        </div>
        <button
          className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition hover:border-blue-300 hover:text-blue-600 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
          onClick={onRefresh}
          disabled={loading}
        >
          <Radio className={`h-3.5 w-3.5 ${loading ? 'animate-pulse' : ''}`} />
          刷新分布
        </button>
      </div>

      {loading && !browseData && (
        <div className="flex items-center justify-center rounded-2xl border border-slate-200 bg-white py-10 text-sm text-slate-400 dark:border-slate-700 dark:bg-slate-800">
          <div className="mr-3 h-5 w-5 animate-spin rounded-full border-2 border-blue-200 border-b-blue-500" />
          正在同步流量分布...
        </div>
      )}

      {browseData && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className={`grid grid-cols-1 gap-4 lg:grid-cols-2 transition-opacity duration-200 ${loading ? 'opacity-65' : 'opacity-100'}`}
        >
          {browseData.sceneSourceList?.length > 0 && (
            <DistributionCard
              title="来源分布"
              caption="平台流量来源"
              items={browseData.sceneSourceList}
              icon={Radio}
              emptyMessage="暂无平台来源画像，需接入闲鱼罗盘数据"
            />
          )}
          <DistributionCard
            title="商品分布"
            caption="按商品名称统计"
            items={browseData.itemCateList || []}
            labelWidth="w-28"
            icon={Package}
            emptyMessage="当前周期暂无商品同步数据"
          />
          <DistributionCard
            title="活跃时段"
            caption="买家咨询活跃时间"
            items={browseData.buyerActiveList || []}
            icon={MessageCircle}
            emptyMessage="当前周期暂无本地咨询记录"
          />
          {browseData.buyerProvinceList?.length > 0 && (
            <DistributionCard
              title="地域分布"
              caption="买家地域画像"
              items={browseData.buyerProvinceList}
              icon={MapPin}
              emptyMessage="暂无平台地域画像，需接入闲鱼罗盘数据"
            />
          )}
        </motion.div>
      )}
      {loading && browseData && (
        <p className="text-right text-xs text-slate-400 dark:text-slate-500">正在更新流量分布...</p>
      )}
    </section>
  )
}
