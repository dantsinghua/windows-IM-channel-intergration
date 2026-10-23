/** 工作台机构配色。光大沿用现有紫黄，南银主体红与存款端一致：#B01C24。 */
export type BrandId = 'ceb' | 'ny'

export interface BrandTheme {
  id: BrandId
  label: string
  primary: string
  primaryStrong: string
  text: string
  textSecondary: string
  bg: string
  border: string
  accent: string
  headerBg: string
  rowHover: string
  primaryShadow: string
}

export const BRANDS: Record<BrandId, BrandTheme> = {
  ceb: {
    id: 'ceb',
    label: '光大理财',
    primary: '#7547A8',
    primaryStrong: '#583180',
    text: '#272131',
    textSecondary: '#706878',
    bg: '#F5F4F7',
    border: '#E6E2EC',
    accent: '#F2AD38',
    headerBg: '#F6F4F9',
    rowHover: '#FAF7FF',
    primaryShadow: '0 4px 12px #7547a824',
  },
  ny: {
    id: 'ny',
    label: '南银理财',
    primary: '#B01C24',
    primaryStrong: '#87141A',
    text: '#2B2320',
    textSecondary: '#6E6258',
    bg: '#F6F1EC',
    border: '#E7DCD4',
    accent: '#C4A574',
    headerBg: '#F8F1EA',
    rowHover: '#FBF6F1',
    primaryShadow: '0 4px 12px #b01c2424',
  },
}

export function brandOf(value: string | null | undefined): BrandId {
  return value === 'ny' ? 'ny' : 'ceb'
}

export function antTheme(brand: BrandId | string | null | undefined) {
  const item = BRANDS[brandOf(typeof brand === 'string' ? brand : null)]
  return {
    token: {
      colorPrimary: item.primary,
      colorInfo: item.primary,
      colorText: item.text,
      colorTextSecondary: item.textSecondary,
      colorBgLayout: item.bg,
      colorBorder: item.border,
      borderRadius: 10,
      fontSize: 14,
      controlHeight: 38,
      fontFamily: '-apple-system, "Segoe UI", "Microsoft YaHei", sans-serif',
    },
    components: {
      Button: { primaryShadow: item.primaryShadow },
      Table: { headerBg: item.headerBg, rowHoverBg: item.rowHover },
      Modal: { borderRadiusLG: 24 },
      Drawer: { paddingLG: 28 },
    },
  }
}
