import {beforeEach,afterEach,expect,it,vi} from "vitest"
import {createPinia,setActivePinia} from "pinia"
import {shallowMount,flushPromises} from "@vue/test-utils"
import {useSessionStore} from "@/stores/session"
import {useResourcesStore} from "@/stores/resources"
import {useAccountsStore} from "@/stores/accounts"
import {systemApi} from "@/api/client"
import {ALERT_CODES} from "@/i18n/zh-CN/codes"
import AcctNewPage from "@/pages/acct/AcctNewPage.vue"
vi.mock("vue-router",()=>({useRoute:()=>({query:{ch:"wechat"}}),useRouter:()=>({push:vi.fn()})}))
vi.mock("ant-design-vue",()=>({message:{error:vi.fn(),success:vi.fn(),info:vi.fn(),warning:vi.fn()}}))
let w: ReturnType<typeof shallowMount>|undefined
beforeEach(()=>{setActivePinia(createPinia());window.sessionStorage.clear()})
afterEach(()=>{w?.unmount();vi.restoreAllMocks();vi.unstubAllGlobals()})
it("health unknown is preserved and is not a logged-out Windows user",async()=>{
 vi.spyOn(systemApi,"health").mockResolvedValue({ok:true,agent:true,dockerd:true,winagent:{online:true,version:"1",user_agent:null}})
 const s=useSessionStore();await s.pingAgent()
 expect(s.userAgentOnline).toBeNull();expect(s.wechatDisabled).toBe(true)
 vi.spyOn(useResourcesStore(),"load").mockResolvedValue(undefined)
 vi.spyOn(useAccountsStore(),"load").mockResolvedValue(undefined)
 vi.stubGlobal("qt",{wa:{invoke:vi.fn().mockRejectedValue(new Error("synthetic 401"))}})
 w=shallowMount(AcctNewPage,{global:{renderStubDefaultSlot:true}});await flushPromises()
 expect(w.text()).toContain("\u65e0\u6cd5\u786e\u8ba4\u4f1a\u8bdd\u4ee3\u7406\u72b6\u6001")
 expect(w.text()).not.toContain("\u8bf7\u5148\u767b\u5f55 Windows \u684c\u9762")
})
it("authentication and session-offline codes have actionable notification text",()=>{
 expect(ALERT_CODES.WINAGENT_AUTH_FAILED?.zh).toContain("\u8ba4\u8bc1")
 expect(ALERT_CODES.WINAGENT_HEALTH_UNAVAILABLE?.zh).toBeTruthy()
 expect(ALERT_CODES.WINAGENT_USER_OFFLINE?.zh).toContain("\u4f1a\u8bdd\u4ee3\u7406")
})