import { expect, it, vi, afterEach } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import AlertCard from '@/components/AlertCard.vue'
import type { AlertPayload } from '@/api/types'
import { streamUrl } from '@/codec/stream'

afterEach(()=>vi.unstubAllGlobals())
it('resource drift displays event timestamps and numerical evidence',()=>{
  const alert: AlertPayload = {code:'POOL_CALIBRATION_DRIFT',severity:'info',state:'resolved',subject:'pool',title:'synthetic drift',message:'synthetic message',hint_actions:[],first_seen_at:'2026-10-14T01:00:00+08:00',last_seen_at:'2026-10-14T02:00:00+08:00',count:1,evidence:{budget_mb:3174,actual_mb:1177,drift_pct:62.9,threshold_pct:30}}
  const wrapper=shallowMount(AlertCard,{props:{alert}})
  expect(wrapper.text()).toContain('已恢复')
  expect(wrapper.text()).toContain(alert.first_seen_at)
  expect(wrapper.text()).toContain(alert.last_seen_at)
  expect(wrapper.text()).toContain('3174')
  expect(wrapper.text()).toContain('1177')
  expect(wrapper.text()).toContain('30')
  wrapper.unmount()
})
it('unknown calibration evidence is not invented',()=>{
  const alert: AlertPayload = {code:'POOL_CALIBRATION_DRIFT',severity:'info',state:'firing',subject:'pool',title:'synthetic',message:'synthetic',hint_actions:[],first_seen_at:'',last_seen_at:'',count:1}
  const wrapper=shallowMount(AlertCard,{props:{alert}})
  expect(wrapper.text()).not.toContain('NaN')
  expect(wrapper.text()).not.toContain('实占 P95: 0')
  wrapper.unmount()
})
it('screen stream uses trusted Agent origin rather than Vite origin',()=>{
  vi.stubGlobal('qt',{endpoint:{agent:'http://127.0.0.1:17650'}})
  const url=streamUrl('qd01','thumb')
  expect(url).toContain('ws://127.0.0.1:17650/')
  expect(url).not.toContain(':5313/')
})
