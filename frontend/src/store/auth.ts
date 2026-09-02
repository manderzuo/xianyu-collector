import { create } from 'zustand'

type User = { id: number; username: string; nickname?: string; role: string }
type AuthState = { token: string | null; user: User | null; setAuth: (token: string, user: User) => void; logout: () => void }

export const useAuth = create<AuthState>((set) => ({
  token: localStorage.getItem('xr_token'),
  user: JSON.parse(localStorage.getItem('xr_user') || 'null') as User | null,
  setAuth: (token, user) => { localStorage.setItem('xr_token', token); localStorage.setItem('xr_user', JSON.stringify(user)); set({ token, user }) },
  logout: () => { localStorage.removeItem('xr_token'); localStorage.removeItem('xr_user'); set({ token: null, user: null }) },
}))
