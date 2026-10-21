import { execFile } from 'node:child_process'
import { join } from 'node:path'

export type LocalState = 'online' | 'offline' | 'unknown'
export interface RecoveryStatus { service: LocalState; session: LocalState; api: LocalState; code: string }
export interface RecoveryResult { ok: boolean; code: string; status: RecoveryStatus }
export interface ShellSnapshot { service: LocalState; session: LocalState; code: string; identityVerified: boolean }
interface Dependencies {
  platform: string
  run(action: 'status' | 'start'): Promise<ShellSnapshot>
  probeWinAgent(): Promise<boolean | null>
  probeAgent(): Promise<boolean | null>
  refresh(): Promise<unknown>
  delay(): Promise<void>
}
const unknown = (code: string): RecoveryStatus => ({ service: 'unknown', session: 'unknown', api: 'unknown', code })
const state = (value: boolean | null): LocalState => value === null ? 'unknown' : value ? 'online' : 'offline'

export function createWinAgentRecovery(d: Dependencies) {
  async function status(): Promise<RecoveryStatus> {
    if (d.platform !== 'win32') return unknown('UNSUPPORTED_PLATFORM')
    try {
      const [query, sessionProbe, apiProbe] = await Promise.allSettled([d.run('status'), d.probeWinAgent(), d.probeAgent()])
      const api = apiProbe.status === 'fulfilled' ? state(apiProbe.value) : 'unknown'
      if (query.status !== 'fulfilled') return { ...unknown('QUERY_FAILED'), api }
      const scm = query.value
      const session = sessionProbe.status === 'fulfilled' ? sessionProbe.value : null
      return { service: scm.service, session: scm.identityVerified && scm.session === 'online' ? state(session) : scm.session, api, code: scm.code }
    } catch { return unknown('QUERY_FAILED') }
  }
  let pending: Promise<RecoveryResult> | null = null
  function start(): Promise<RecoveryResult> {
    if (pending) return pending
    pending = (async (): Promise<RecoveryResult> => {
      if (d.platform !== 'win32') return { ok: false, code: 'UNSUPPORTED_PLATFORM', status: unknown('UNSUPPORTED_PLATFORM') }
      try {
        const result = await d.run('start')
        if (result.code !== 'OK' || !result.identityVerified) {
          return { ok: false, code: result.code, status: { service: result.service, session: result.session, api: 'unknown', code: result.code } }
        }
        await d.refresh()
        let current = await status()
        for (let n = 0; n < 3 && (current.service !== 'online' || current.session !== 'online'); n++) {
          await d.delay(); await d.refresh(); current = await status()
        }
        const ok = current.service === 'online' && current.session === 'online'
        return { ok, code: ok ? 'STARTED' : 'STARTING', status: current }
      } catch { return { ok: false, code: 'START_FAILED', status: unknown('START_FAILED') } }
    })().finally(() => { pending = null })
    return pending
  }
  return { status, start }
}

/** Fixed scripts only. No renderer input reaches a service name, path or command. */
export function buildRecoveryScript(action: 'status' | 'start'): string {
  const query = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$serviceState = 'unknown'; $sessionState = 'unknown'
function Emit($code, $verified) {
  @{service=$serviceState;session=$sessionState;code=$code;identityVerified=[bool]$verified} | ConvertTo-Json -Compress
  exit
}
try { $svc = Get-CimInstance Win32_Service -Filter "Name='QTradeWinAgent'" } catch { Emit 'SCM_QUERY_FAILED' $false }
if (-not $svc) { $serviceState='offline'; Emit 'SERVICE_NOT_INSTALLED' $false }
$serviceState = if ($svc.State -eq 'Running') {'online'} elseif ($svc.State -eq 'Stopped') {'offline'} else {'unknown'}
$identity = $svc.PathName
if ($identity -notmatch '^\s*(?:"(?<exe>[^"]+)"|(?<exe>\S+))\s+--service\s+--root\s+(?:"(?<root>[^"]+)"|(?<root>\S+))\s+--install-user-sid\s+(?<sid>S-\d+(?:-\d+)+)\s*$') { Emit 'IDENTITY_MISMATCH' $false }
try {
  $root = [IO.Path]::GetFullPath($Matches['root']).TrimEnd('\')
  $serviceExe = [IO.Path]::GetFullPath($Matches['exe'])
  $installedSid = $Matches['sid']
  if ($root -notmatch '^[A-Za-z]:\\' -or $serviceExe -ine (Join-Path $root 'app\qtrade-winagent-svc.exe')) { Emit 'IDENTITY_MISMATCH' $false }
  if (-not (Test-Path -LiteralPath $serviceExe -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $root 'winagent.toml') -PathType Leaf)) { Emit 'INSTALLATION_INCOMPLETE' $false }
  if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $installedSid) { Emit 'SESSION_IDENTITY_MISMATCH' $false }
  $userExe = Join-Path $root 'app\qtrade-winagent-user.exe'
  if (-not (Test-Path -LiteralPath $userExe -PathType Leaf)) { Emit 'INSTALLATION_INCOMPLETE' $false }
  $sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
  $users = @(Get-CimInstance Win32_Process -Filter "Name='qtrade-winagent-user.exe'" | Where-Object {$_.SessionId -eq $sessionId})
  $matching = @($users | Where-Object {$_.ExecutablePath -ieq $userExe})
  if ($users.Count -ne $matching.Count) { Emit 'SESSION_PROCESS_UNVERIFIED' $false }
  if ($matching.Count -gt 1) { Emit 'MULTIPLE_SESSION_AGENTS' $false }
  $sessionState = if ($matching.Count -eq 1) {'online'} else {'offline'}
} catch { Emit 'IDENTITY_QUERY_FAILED' $false }
`
  if (action === 'status') return query + "\nEmit 'OK' $true\n"
  return query + String.raw`
if ($svc.State -ne 'Running') {
  if ($svc.State -ne 'Stopped') { Emit 'SERVICE_PENDING' $true }
  $identity64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($identity))
  $elevated = "try { if ((Get-CimInstance Win32_Service -Filter ""Name='QTradeWinAgent'"").PathName -cne [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + $identity64 + "'))) { exit 2 }; if ((Get-Service -Name 'QTradeWinAgent').Status -ne 'Running') { Start-Service -Name 'QTradeWinAgent' -ErrorAction Stop }; exit 0 } catch { exit 1 }"
  $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevated))
  try {
    $e = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand',$encoded) -Verb RunAs -Wait -PassThru
    if ($e.ExitCode -eq 2) { Emit 'IDENTITY_CHANGED' $false }
    if ($e.ExitCode -ne 0) { Emit 'SERVICE_START_FAILED' $true }
  } catch {
    if ($_.Exception.NativeErrorCode -eq 1223 -or $_.Exception.InnerException.NativeErrorCode -eq 1223) { Emit 'UAC_CANCELLED' $true }
    Emit 'ELEVATION_FAILED' $true
  }
}
$svc = Get-CimInstance Win32_Service -Filter "Name='QTradeWinAgent'"
if ($svc.PathName -cne $identity) { Emit 'IDENTITY_CHANGED' $false }
$serviceState = if ($svc.State -eq 'Running') {'online'} else {'unknown'}
if ($serviceState -ne 'online') { Emit 'SERVICE_START_FAILED' $true }
if ($matching.Count -eq 0) {
  try {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $userExe
    $info.Arguments = '--root "' + $root + '" --config "' + (Join-Path $root 'winagent.toml') + '"'
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $true
    [Diagnostics.Process]::Start($info) | Out-Null
    $sessionState = 'unknown'
  } catch { Emit 'SESSION_START_FAILED' $true }
}
Emit 'OK' $true
`
}

export function runRecoveryPowerShell(action: 'status' | 'start'): Promise<ShellSnapshot> {
  const script = Buffer.from(buildRecoveryScript(action), 'utf16le').toString('base64')
  const executable = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  return new Promise((resolve, reject) => {
    execFile(executable, ['-NoProfile', '-NonInteractive', '-EncodedCommand', script], { windowsHide: true, timeout: action === 'start' ? 180_000 : 15_000, maxBuffer: 65536 }, (error, stdout) => {
      if (error) { reject(new Error('POWERSHELL_FAILED')); return }
      try {
        const data = JSON.parse(stdout.trim()) as ShellSnapshot
        if (!['online', 'offline', 'unknown'].includes(data.service) || !['online', 'offline', 'unknown'].includes(data.session) || typeof data.identityVerified !== 'boolean' || typeof data.code !== 'string') throw new Error()
        resolve(data)
      } catch { reject(new Error('POWERSHELL_RESPONSE_INVALID')) }
    })
  })
}

interface Ipc { handle(name: string, fn: (event: unknown, ...args: unknown[]) => unknown): unknown }
export function registerRecoveryIpc(ipc: Ipc, recovery: ReturnType<typeof createWinAgentRecovery>, trusted: (event: unknown) => boolean): void {
  for (const op of ['status', 'start'] as const) {
    ipc.handle(`qt:localService.${op}`, (event, ...args) => {
      if (!trusted(event)) throw new Error('UNTRUSTED_SENDER')
      if (args.length) throw new Error('INVALID_ARGUMENTS')
      return recovery[op]()
    })
  }
}
