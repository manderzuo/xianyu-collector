import axios, { AxiosError, AxiosInstance, AxiosRequestConfig, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/store/authStore'
import { notifyVipContentIfNeeded } from '@/utils/vipContent'

// 创建 axios 实例
const request: AxiosInstance = axios.create({
  baseURL: '',
  timeout: 90000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 是否正在刷新Token
let isRefreshing = false
// 等待刷新完成的请求队列
let refreshSubscribers: Array<{ resolve: (token: string) => void; reject: (error: unknown) => void }> = []

// 添加请求到队列
const subscribeTokenRefresh = (resolve: (token: string) => void, reject: (error: unknown) => void) => {
  refreshSubscribers.push({ resolve, reject })
}

// 通知所有等待的请求
const onRefreshed = (token: string) => {
  refreshSubscribers.forEach(({ resolve }) => resolve(token))
  refreshSubscribers = []
}

const onRefreshFailed = (error: unknown) => {
  refreshSubscribers.forEach(({ reject }) => reject(error))
  refreshSubscribers = []
}

// 请求拦截器
request.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('auth_token') || localStorage.getItem('xr_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    // FormData 需要让浏览器自动设置 multipart/form-data + boundary，不能强制指定 JSON
    if (config.data instanceof FormData) {
      delete config.headers['Content-Type']
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器
request.interceptors.response.use(
  (response: AxiosResponse) => {
    return response
  },
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean }

    notifyVipContentIfNeeded(error.response?.status, error.response?.data)

    // 如果是401错误且不是刷新Token接口
    if (error.response?.status === 401 && originalRequest && !originalRequest._retry) {
      // 如果是刷新Token接口失败，直接退出登录
      if (originalRequest.url?.includes('/auth/refresh')) {
        useAuthStore.getState().clearAuth()
        return Promise.reject(error)
      }

      // 标记该请求已重试过
      originalRequest._retry = true

      if (!isRefreshing) {
        isRefreshing = true
        const refreshToken = localStorage.getItem('refresh_token')

        if (!refreshToken) {
          // 没有refresh token，直接退出登录
          useAuthStore.getState().clearAuth()
          isRefreshing = false
          onRefreshFailed(error)
          return Promise.reject(error)
        }

        try {
          // 调用刷新Token接口
          const response = await axios.post('/api/v1/auth/refresh', {}, {
            headers: {
              Authorization: `Bearer ${refreshToken}`
            }
          })

          const data = response.data?.data ?? response.data
          const nextToken = data?.access_token || data?.token
          const nextRefreshToken = data?.refresh_token || refreshToken
          if (response.data?.success && nextToken) {
            // 更新Token和用户信息
            const authStore = useAuthStore.getState()
            authStore.updateTokens(nextToken, nextRefreshToken)
            // 更新用户信息（如果返回了）
            if (data.user_id && data.username !== undefined && data.is_admin !== undefined) {
              authStore.updateUser({
                user_id: data.user_id,
                username: data.username,
                is_admin: data.is_admin,
                account_limit: data.account_limit,
              })
            }
            // 通知所有等待的请求
            onRefreshed(nextToken)
            // 重试原请求
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${nextToken}`
            }
            return request(originalRequest)
          } else {
            // 刷新失败，退出登录
            useAuthStore.getState().clearAuth()
            onRefreshFailed(error)
            return Promise.reject(error)
          }
        } catch (refreshError) {
          // 刷新Token失败，退出登录
          useAuthStore.getState().clearAuth()
          onRefreshFailed(refreshError)
          return Promise.reject(refreshError)
        } finally {
          isRefreshing = false
        }
      } else {
        // 正在刷新Token，将请求加入队列
        return new Promise((resolve, reject) => {
          subscribeTokenRefresh((token: string) => {
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${token}`
            }
            resolve(request(originalRequest))
          }, reject)
        })
      }
    }

    return Promise.reject(error)
  }
)

// 封装 GET 请求
export const get = async <T = unknown>(
  url: string,
  config?: AxiosRequestConfig
): Promise<T> => {
  const response = await request.get<T>(url, config)
  return response.data
}

// 封装 POST 请求
export const post = async <T = unknown>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<T> => {
  const response = await request.post<T>(url, data, config)
  return response.data
}

// 封装 PUT 请求
export const put = async <T = unknown>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<T> => {
  const response = await request.put<T>(url, data, config)
  return response.data
}

// 封装 DELETE 请求
export const del = async <T = unknown>(
  url: string,
  config?: AxiosRequestConfig
): Promise<T> => {
  const response = await request.delete<T>(url, config)
  return response.data
}

// 封装 PATCH 请求
export const patch = async <T = unknown>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<T> => {
  const response = await request.patch<T>(url, data, config)
  return response.data
}

// 复用 utils/apiError.ts 的实现，保留 re-export 以兼容 `import { getApiErrorMessage } from '@/utils/request'` 的旧调用点
export { getApiErrorMessage } from './apiError'

export default request
