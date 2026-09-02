/**
 * 更新详情弹窗
 *
 * 功能：
 * 1. 展示当前版本与最新远程版本的对比
 * 2. 分行渲染更新说明（支持 \n 换行）
 * 3. 提供关闭更新提示的操作
 *
 * 交互约束：
 * - 按项目规则，弹窗不支持点击空白处关闭，只能点右上角 X 或底部按钮关闭
 *
 */
import { ArrowUpCircle, CheckCircle, X } from 'lucide-react'

import type { VersionCheckResult } from '@/api/version'

interface UpdateModalProps {
  /** 远程最新版本信息，必填，为 null/undefined 时组件不渲染 */
  info: VersionCheckResult
  /** 关闭弹窗回调 */
  onClose: () => void
}

/**
 * 将更新说明字符串按换行切分为非空行数组
 */
function splitDescriptionLines(description: string): string[] {
  return description
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.length > 0)
}

export function UpdateModal({ info, onClose }: UpdateModalProps) {
  const changeLines = splitDescriptionLines(info.description)

  return (
    <div className="modal-overlay">
      <div className="modal-content max-w-lg">
        <div className="modal-header">
          <h2 className="modal-title flex items-center gap-2">
            <ArrowUpCircle className="w-5 h-5 text-amber-500" />
            发现新版本
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 hover:bg-gray-100 dark:hover:bg-slate-700 rounded-lg"
          >
            <X className="w-4 h-4 text-gray-500" />
          </button>
        </div>
        <div className="modal-body space-y-4">
          <div className="flex items-center justify-between p-4 bg-gradient-to-r from-amber-50 to-orange-50 dark:from-amber-900/20 dark:to-orange-900/20 rounded-lg">
            <div>
              <p className="text-sm text-slate-500 dark:text-slate-400">当前版本</p>
              <p className="text-lg font-bold text-slate-700 dark:text-slate-200">
                v{info.current_version}
              </p>
            </div>
            <div className="text-2xl text-slate-400">→</div>
            <div>
              <p className="text-sm text-slate-500 dark:text-slate-400">最新版本</p>
              <p className="text-lg font-bold text-emerald-600 dark:text-emerald-400">
                v{info.remote_version}
              </p>
            </div>
          </div>

          {changeLines.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                更新内容：
              </h3>
              <ul className="space-y-1.5">
                {changeLines.map((line, index) => (
                  <li
                    key={index}
                    className="flex items-start gap-2 text-sm text-slate-600 dark:text-slate-400"
                  >
                    <CheckCircle className="w-4 h-4 text-emerald-500 mt-0.5 flex-shrink-0" />
                    <span>{line}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="p-3 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
            <p className="text-xs text-blue-600 dark:text-blue-400">
              <strong>提示：</strong>更新内容由系统服务提供，确认后即可关闭此提示。
            </p>
          </div>
        </div>
        <div className="modal-footer">
          <button
            type="button"
            onClick={onClose}
            className="btn-ios-secondary"
          >
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}
