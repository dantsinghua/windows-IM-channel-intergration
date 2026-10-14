import {afterEach,beforeEach,expect,it,vi} from "vitest"
import {flushPromises,shallowMount,type VueWrapper} from "@vue/test-utils"
import {createPinia,setActivePinia} from "pinia"
import AcctNewPage from "@/pages/acct/AcctNewPage.vue"
import {accountsApi,resourcesApi} from "@/api/client"
import {useAccountsStore} from "@/stores/accounts"
import type {Account,ResourcePool} from "@/api/types"
import {acctNew as T} from "@/testids"
vi.mock("vue-router",()=>({useRoute:()=>({query:{ch:"wechat"}}),useRouter:()=>({push:vi.fn(),replace:vi.fn()})}))
vi.mock("ant-design-vue",()=>({message:{error:vi.fn(),success:vi.fn(),info:vi.fn(),warning:vi.fn()}}))
const stubs={...Object.fromEntries(["a-steps","a-step","a-input","a-select","a-checkbox","a-modal","a-popconfirm"].map(n=>[n,true])),"a-button":{props:["disabled","loading"],template:"<button :disabled=\"disabled || loading\"><slot /></button>"}}
let wrapper:VueWrapper|undefined
let keysReady=true
function account(state:Account["state"]="starting"):Account {return {id:"wx01",channel:"wechat",host:"windows",label:"synthetic",state,state_code:state==="degraded"?"KEY_FAIL":"",state_reason:"Synthetic read failure",error_since_ms:null,enabled:true,auto_recover:true,deleted_ms:null,runtime:{},capabilities:[],quota_mb:512}}
beforeEach(()=>{
 setActivePinia(createPinia());keysReady=true;window.sessionStorage.clear()
 vi.stubGlobal("fetch",vi.fn().mockRejectedValue(new Error("No real network")))
 vi.stubGlobal("qt",{wa:{invoke:vi.fn(async(method:string)=>method==="wechat.version-match"?{match:"SUPPORTED",current_version:"4.1.12.26",bundled_version:"4.1.12.26"}:{enabled:true,chatlog:{key_ok:keysReady},wechat:{installed:true,version:"4.1.12.26",running:true,logged_in:true}})}})
 vi.spyOn(resourcesApi,"get").mockResolvedValue({pools:{wsl:{total_mb:8192,reserved_mb:0,used_mb:0,free_mb:8192},windows:{total_mb:8192,reserved_mb:0,wechat_mb:0,wechat_slots:{used:0,max:1,holder:"",pending:"",pending_expires_at:null,pending_login_session_id:""}}},realtime:{wsl_anon_mb:0,win_available_mb:8192},quota_mb:{qidian:2048,qq:512,wechat:512},can_add:{qidian:4,qq:16,wechat:1}} as ResourcePool)
 vi.spyOn(useAccountsStore(),"load").mockImplementation(async()=>{useAccountsStore().upsert(account())})
 vi.spyOn(accountsApi,"switchNew").mockResolvedValue({target:"wx01",login_session_id:"round-1"} as never)
 vi.spyOn(accountsApi,"start").mockResolvedValue({state:"starting"})
})
afterEach(()=>{wrapper?.unmount();wrapper=undefined;vi.restoreAllMocks();vi.unstubAllGlobals()})
async function start(){
 wrapper=shallowMount(AcctNewPage,{global:{stubs}});await flushPromises()
 for(let i=0;i<2;i++){await wrapper.get(`[data-testid="${T.next}"]`).trigger("click");await flushPromises()}
 await wrapper.get(`[data-testid="${T.wxSwitchConfirm}"]`).trigger("click");await flushPromises()
 useAccountsStore().upsert(account("running"));await flushPromises()
 useAccountsStore().upsert(account("degraded"));await flushPromises()
}
it("valid keys plus a read failure do not recommend rekey or reinstall",async()=>{
 await start()
 expect(wrapper!.text()).toContain("\u6d88\u606f\u8bfb\u53d6")
 expect(wrapper!.find(`[data-testid="${T.wxKeyRetry}"]`).exists()).toBe(false)
 expect(wrapper!.find(`[data-testid="${T.wxKeyReinstall}"]`).exists()).toBe(false)
 expect(wrapper!.find(`[data-testid="qt-wechat-read-recheck"]`).exists()).toBe(true)
})
it("missing keys still offer the existing key recovery action",async()=>{
 keysReady=false;await start()
 expect(wrapper!.find(`[data-testid="${T.wxKeyRetry}"]`).exists()).toBe(true)
})
