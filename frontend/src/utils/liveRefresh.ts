import { useEffect, useRef } from 'react'

export type LiveRefreshTopic = 'all' | 'accounts' | 'items' | 'orders' | 'dashboard' | 'messages' | 'analytics'

const LIVE_REFRESH_EVENT = 'xr:live-refresh'

/** 通知当前页面立即刷新指定数据域，不触发整页 reload。 */
export function emitLiveRefresh(topic: LiveRefreshTopic = 'all') {
  window.dispatchEvent(new CustomEvent(LIVE_REFRESH_EVENT, { detail: { topic } }))
}

/** 页面获得焦点、回到前台和定时轮询时复用页面自己的增量加载逻辑。 */
export function useLiveRefresh(
  refresh: () => void | Promise<void>,
  options: {
    topics?: LiveRefreshTopic[]
    intervalMs?: number
    enabled?: boolean
  } = {},
) {
  const refreshRef = useRef(refresh)
  const topics = options.topics ?? ['all']
  const topicsKey = topics.join('|')
  const intervalMs = options.intervalMs ?? 30000
  const enabled = options.enabled ?? true

  useEffect(() => {
    refreshRef.current = refresh
  }, [refresh])

  useEffect(() => {
    if (!enabled) return undefined

    const shouldRefresh = (event: Event) => {
      const topic = (event as CustomEvent<{ topic?: LiveRefreshTopic }>).detail?.topic
      if (!topic || topic === 'all' || topics.includes(topic)) {
        void refreshRef.current()
      }
    }
    const handleFocus = () => void refreshRef.current()
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') void refreshRef.current()
    }

    window.addEventListener(LIVE_REFRESH_EVENT, shouldRefresh)
    window.addEventListener('focus', handleFocus)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    const timer = window.setInterval(() => void refreshRef.current(), intervalMs)

    return () => {
      window.removeEventListener(LIVE_REFRESH_EVENT, shouldRefresh)
      window.removeEventListener('focus', handleFocus)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      window.clearInterval(timer)
    }
  }, [enabled, intervalMs, topicsKey])
}
