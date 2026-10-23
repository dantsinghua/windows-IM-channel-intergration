import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { antTheme, brandOf } from '@/theme/brands'

describe('机构配色', () => {
  it('未指定时用光大理财的紫', () => {
    expect(brandOf(null)).toBe('ceb')
    expect(antTheme('ceb').token.colorPrimary).toBe('#7547A8')
    expect(antTheme(undefined).token.colorPrimary).toBe('#7547A8')
  })

  it('南银理财用红，不沿用光大紫', () => {
    expect(brandOf('ny')).toBe('ny')
    expect(antTheme('ny').token.colorPrimary).toBe('#B01C24')
    expect(antTheme('ny').token.colorPrimary).not.toBe(antTheme('ceb').token.colorPrimary)
  })

  it('架构图底用主题色浅渐变和毛玻璃', () => {
    const vue = readFileSync(resolve(process.cwd(), 'src/components/SystemTopology.vue'), 'utf8')
    expect(vue).toContain('backdrop-filter:blur(18px)')
    expect(vue).toContain('color-mix(in srgb,var(--qt-primary)')
    expect(vue).not.toContain('#eae0f6')
  })

  it('首页强调条和账号数字跟随主题色', () => {
    const dash = readFileSync(resolve(process.cwd(), 'src/pages/dash/DashPage.vue'), 'utf8')
    expect(dash).toContain('.purple{background:var(--qt-primary)}')
    expect(dash).not.toContain('#b394d1')
    expect(dash).not.toContain('#8b69ad')
  })

  it('历史消息和偏好设置跟随主题色', () => {
    const msg = readFileSync(resolve(process.cwd(), 'src/pages/msg/MsgPage.vue'), 'utf8')
    const set = readFileSync(resolve(process.cwd(), 'src/pages/set/SetPage.vue'), 'utf8')
    const tag = readFileSync(resolve(process.cwd(), 'src/components/ChannelTag.vue'), 'utf8')
    expect(msg).not.toContain('#e8def5')
    expect(msg).toContain('var(--qt-msg-hover)')
    expect(msg).not.toContain('#fcfaff')
    expect(msg).not.toContain('#ded8f2')
    expect(tag).toContain('var(--qt-primary)')
    expect(set).not.toContain('#8e72a6')
    expect(set).toContain('.ant-switch-checked')
  })

  it('滚动条是圆角，南银红与存款端 #B01C24 一致', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/theme/tokens.css'), 'utf8')
    expect(css).toContain('border-radius: 999px')
    expect(css).toContain('#B01C24')
    expect(css).not.toContain('#8A2823')
  })
})
