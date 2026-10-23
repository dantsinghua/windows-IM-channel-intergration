import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import { useResourcesStore } from '@/stores/resources'
import { useAccountsStore } from '@/stores/accounts'
import { acctNew as T } from '@/testids'
const state=vi.hoisted(()=>({query:{ch:'wechat'}}))
vi.mock('vue-router',()=>({useRoute:()=>state,useRouter:()=>({push:vi.fn()})}))
vi.mock('ant-design-vue',()=>({message:{error:vi.fn(),success:vi.fn(),info:vi.fn(),warning:vi.fn()}}))
let wrapper: ReturnType<typeof shallowMount> | undefined
beforeEach(()=>{
  window.sessionStorage.clear()
  setActivePinia(createPinia())
  vi.spyOn(useResourcesStore(),'load').mockResolvedValue(undefined)
  vi.spyOn(useAccountsStore(),'load').mockResolvedValue(undefined)
})
afterEach(()=>{wrapper?.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals()})
it('does not render an unreachable module as disabled',async()=>{
  vi.stubGlobal('qt',{wa:{invoke:vi.fn().mockRejectedValue(new Error('synthetic service offline'))}})
  wrapper=shallowMount(AcctNewPage,{global:{renderStubDefaultSlot:true,stubs:{'a-button':{props:['disabled'],template:'<button :disabled="disabled"><slot /></button>'}}}})
  await flushPromises()
  expect(wrapper.find(`[data-testid="${T.wxModuleStatus}"]`).text()).toContain('无法确认')
  expect(wrapper.text()).toContain('synthetic service offline')
})
it('still distinguishes an explicitly disabled module',async()=>{
  vi.stubGlobal('qt',{wa:{invoke:vi.fn().mockResolvedValue({enabled:false})}})
  wrapper=shallowMount(AcctNewPage,{global:{renderStubDefaultSlot:true}})
  await flushPromises()
  expect(wrapper.find(`[data-testid="${T.wxModuleStatus}"]`).text()).toContain('未启用')
})
it('still distinguishes an enabled module',async()=>{
  vi.stubGlobal('qt',{wa:{invoke:vi.fn().mockResolvedValue({enabled:true})}})
  wrapper=shallowMount(AcctNewPage,{global:{renderStubDefaultSlot:true}})
  await flushPromises()
  expect(wrapper.find(`[data-testid="${T.wxModuleStatus}"]`).text()).toContain('已启用')
})
