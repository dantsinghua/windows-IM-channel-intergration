// @vitest-environment node
import { beforeEach, afterEach, expect, it, vi } from "vitest"
import { createPipeClient } from "../../electron/main/winagent-pipe"
const h=vi.hoisted(()=>({ reply: {} as Record<string, unknown>, requests: [] as string[] }))
vi.mock("node:net",async()=>{
 const {EventEmitter}=await import("node:events")
 return {connect:()=>{
  const s=new EventEmitter() as InstanceType<typeof EventEmitter> & {write:(x:string)=>void;destroy:()=>void}
  s.write=(x)=>{h.requests.push(x);s.emit("data",Buffer.from(JSON.stringify(h.reply)));s.emit("close")}
  s.destroy=()=>s.emit("close");queueMicrotask(()=>s.emit("connect"));return s
 }}
})
beforeEach(()=>{ h.requests=[];h.reply={agent_token:null,agent_base_url:null,winagent_token:"REAL_PIPE_C",expires_at:Date.now()+100000};vi.stubEnv("QT_DEV_TOKEN","DEV_AGENT_ONLY") })
afterEach(()=>{vi.unstubAllEnvs();vi.restoreAllMocks()})
it("Windows development credentials remain distinct and do not fall back",async()=>{
 const b=await createPipeClient({isPackaged:false,platform:"win32"}).fetchTokens()
 expect(b.agent_token).toBe("DEV_AGENT_ONLY");expect(b.winagent_token).toBe("REAL_PIPE_C")
 expect(b.agent_base_url).toBeUndefined()
 expect(h.requests.map(x=>JSON.parse(x))).toEqual([{op:"issue_console_token"}])
})
it("Windows development pipe failure never presents the Agent token as a WinAgent token",async()=>{
 h.reply={error:"NOT_READY"}
 await expect(createPipeClient({isPackaged:false,platform:"win32"}).fetchTokens()).rejects.toThrow()
})
it("packaged application ignores inherited development credentials",async()=>{
 expect(createPipeClient({isPackaged:true,platform:"win32"}).describe()).toMatch(/^named-pipe /)
 const b=await createPipeClient({isPackaged:true,platform:"win32"}).fetchTokens()
 expect(b.agent_token).toBeNull();expect(b.winagent_token).toBe("REAL_PIPE_C")
})
it("non-Windows explicit mock remains usable for isolated development",async()=>{
 expect((await createPipeClient({isPackaged:false,platform:"linux"}).fetchTokens()).agent_token).toBe("DEV_AGENT_ONLY")
})