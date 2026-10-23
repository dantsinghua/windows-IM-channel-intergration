/** `ui` store:主题、侧栏折叠、当前专注账号、画面性能档(存 console.toml / LOCALAPPDATA) */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { brandOf, type BrandId } from '@/theme/brands'

export type ThemeMode = 'light' | 'dark' | 'system'
export type PerfProfile = 'focus30' | 'focus15' | 'thumb10'

export const useUiStore = defineStore('ui', () => {
  // v1 只交付浅色,深色留 M5(P-05)
  const theme = ref<ThemeMode>('light')
  const brand = ref<BrandId>('ceb')
  const navCollapsed = ref(false)
  const focusedAccountId = ref<string | null>(null)
  const perfProfile = ref<PerfProfile>('focus30')
  const notifyEnabled = ref(true)
  const autoLaunch = ref(true)
  const trayOnClose = ref(true)
  const alertDrawerOpen = ref(false)

  async function loadFromConfig(): Promise<void> {
    const stored = typeof localStorage === 'undefined' ? null : localStorage.getItem('qtrade.brand')
    if (stored) brand.value = brandOf(stored)
    const cfg = await window.qt?.config.read()
    if (!cfg) return
    theme.value = (cfg.ui?.theme as ThemeMode) ?? 'light'
    if (!stored) brand.value = brandOf(cfg.ui?.brand as string | undefined)
    navCollapsed.value = !!cfg.ui?.nav_collapsed
    perfProfile.value = (cfg.screen?.default_focus_profile as PerfProfile) ?? 'focus30'
    notifyEnabled.value = cfg.notify?.enabled !== false
    autoLaunch.value = cfg.app?.auto_launch !== false
    trayOnClose.value = cfg.app?.minimize_to_tray_on_close !== false
  }

  async function persist(patch: Record<string, Record<string, unknown>>): Promise<void> {
    await window.qt?.config.patch(patch)
  }

  function applyTheme(): void {
    if (typeof document === 'undefined') return
    document.documentElement.setAttribute('data-theme', theme.value === 'dark' ? 'dark' : 'light')
    document.documentElement.setAttribute('data-brand', brand.value)
  }

  function setBrand(next: BrandId): void {
    brand.value = next
    if (typeof localStorage !== 'undefined') localStorage.setItem('qtrade.brand', next)
    applyTheme()
  }

  function toggleNav(): void {
    navCollapsed.value = !navCollapsed.value
    void persist({ ui: { nav_collapsed: navCollapsed.value } })
  }

  return {
    theme, brand, navCollapsed, focusedAccountId, perfProfile, notifyEnabled, autoLaunch, trayOnClose, alertDrawerOpen,
    loadFromConfig, persist, applyTheme, setBrand, toggleNav,
  }
})
