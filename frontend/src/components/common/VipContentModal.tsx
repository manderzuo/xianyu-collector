import { AnimatePresence, motion } from 'framer-motion'
import { Crown, X } from 'lucide-react'
import { useUIStore } from '@/store/uiStore'

export function VipContentModal() {
  const { vipContentModalOpen, hideVipContentModal } = useUIStore()

  return (
    <AnimatePresence>
      {vipContentModalOpen && (
        <div className="fixed inset-0 z-[220] flex items-center justify-center" role="dialog" aria-modal="true" aria-labelledby="vip-content-title">
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="absolute inset-0 bg-black/50 backdrop-blur-sm" />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 16 }}
            className="relative mx-4 w-full max-w-sm rounded-2xl border border-amber-200 bg-white shadow-2xl dark:border-amber-900/60 dark:bg-slate-800"
          >
            <button type="button" onClick={hideVipContentModal} className="absolute right-3 top-3 rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-700 dark:hover:text-slate-200" aria-label="关闭">
              <X className="h-4 w-4" />
            </button>
            <div className="p-7 text-center">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300">
                <Crown className="h-6 w-6" />
              </div>
              <h2 id="vip-content-title" className="mb-2 text-lg font-semibold text-slate-900 dark:text-white">VIP专属内容</h2>
              <p className="text-slate-600 dark:text-slate-300">以上属于VIP内容</p>
            </div>
            <div className="px-7 pb-7">
              <button type="button" onClick={hideVipContentModal} className="w-full rounded-lg bg-amber-500 px-4 py-2.5 font-medium text-white transition-colors hover:bg-amber-600">知道了</button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
