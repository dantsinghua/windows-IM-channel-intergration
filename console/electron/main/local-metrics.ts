import { lstat, opendir, realpath } from 'node:fs/promises'
import { freemem, totalmem } from 'node:os'
import { isAbsolute, join, parse, resolve } from 'node:path'
import type { ClientStorageUsage, HostMemoryUsage, LocalMetricsSnapshot, StorageSampleError } from '../../src/types/local-metrics'

export interface StorageScanOptions {
  timeoutMs?: number
  cacheMs?: number
  maxEntries?: number
  maxDepth?: number
}

class ScanFailure extends Error {
  constructor(readonly code: StorageSampleError) { super(code) }
}

function unavailable(error: StorageSampleError): ClientStorageUsage {
  return {
    available: false, bytes: null, scope: 'client_directory', measurement: 'file_sizes',
    sampledAt: new Date().toISOString(), excludedLinks: 0, error,
  }
}

/**
 * root 只由主进程自身决定；渲染层不能传路径。扫描只读取文件元数据。
 * 超时后立即返回未知，正在进行的单个 OS 读取结束后即退出，不继续遍历。
 */
export function createClientStorageSampler(root: string, options: StorageScanOptions = {}): () => Promise<ClientStorageUsage> {
  const timeoutMs = options.timeoutMs ?? 5000
  const cacheMs = options.cacheMs ?? 30000
  const maxEntries = options.maxEntries ?? 100000
  const maxDepth = options.maxDepth ?? 64
  const fixedRoot = resolve(root)
  let cached: ClientStorageUsage | null = null
  let expiresAt = 0
  let request: Promise<ClientStorageUsage> | null = null
  let scanning = false

  async function scan(cancelled: () => boolean): Promise<ClientStorageUsage> {
    const check = (): void => { if (cancelled()) throw new ScanFailure('timeout') }
    if (!isAbsolute(root) || fixedRoot === parse(fixedRoot).root) throw new ScanFailure('invalid_root')
    check()
    const rootStat = await lstat(fixedRoot)
    check()
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) throw new ScanFailure('invalid_root')
    const canonicalRoot = await realpath(fixedRoot)
    check()
    const pending = [{ path: canonicalRoot, depth: 0 }]
    const files = new Set<string>()
    let entries = 0
    let bytes = 0
    let excludedLinks = 0

    while (pending.length) {
      check()
      const current = pending.pop()!
      if (current.depth > maxDepth) throw new ScanFailure('limit_exceeded')
      const directoryStat = await lstat(current.path)
      check()
      if (directoryStat.isSymbolicLink()) { excludedLinks++; continue }
      if (!directoryStat.isDirectory()) throw new ScanFailure('read_failed')
      // A directory changed to a link during the scan must not escape the fixed root.
      const canonicalDirectory = await realpath(current.path)
      check()
      if (canonicalDirectory !== current.path) throw new ScanFailure('read_failed')
      const directory = await opendir(current.path)
      try {
        while (true) {
          check()
          const entry = await directory.read()
          check()
          if (!entry) break
          if (++entries > maxEntries) throw new ScanFailure('limit_exceeded')
          const path = join(current.path, entry.name)
          const stat = await lstat(path, { bigint: true })
          check()
          if (stat.isSymbolicLink()) { excludedLinks++; continue }
          if (stat.isDirectory()) {
            if (current.depth + 1 > maxDepth) throw new ScanFailure('limit_exceeded')
            pending.push({ path, depth: current.depth + 1 })
          } else if (stat.isFile()) {
            const identity = stat.ino > 0n ? `${stat.dev}:${stat.ino}` : null
            if (identity && files.has(identity)) continue
            if (identity) files.add(identity)
            const fileBytes = Number(stat.size)
            if (!Number.isSafeInteger(fileBytes) || fileBytes < 0 || !Number.isSafeInteger(bytes + fileBytes)) {
              throw new ScanFailure('read_failed')
            }
            bytes += fileBytes
          }
        }
      } finally {
        await directory.close()
      }
    }
    check()
    return {
      available: true, bytes, scope: 'client_directory', measurement: 'file_sizes',
      sampledAt: new Date().toISOString(), excludedLinks,
    }
  }

  return () => {
    if (cached && Date.now() < expiresAt) return Promise.resolve(cached)
    if (request) return request
    // A slow OS read from a timed-out scan may still be finishing. Never stack more scans.
    if (scanning) return Promise.resolve(cached ?? unavailable('timeout'))
    scanning = true
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const work = scan(() => cancelled)
      .catch((error: unknown) => unavailable(error instanceof ScanFailure ? error.code : 'read_failed'))
      .finally(() => { scanning = false })
    const timedOut = new Promise<ClientStorageUsage>((resolveTimeout) => {
      timer = setTimeout(() => { cancelled = true; resolveTimeout(unavailable('timeout')) }, timeoutMs)
    })
    request = Promise.race([work, timedOut]).then((result) => {
      clearTimeout(timer)
      cached = result
      expiresAt = Date.now() + cacheMs
      return result
    }).finally(() => { request = null })
    return request
  }
}

/** Windows 客户端宿主物理内存，不把 WSL 的 /proc/meminfo 冒充整机内存。 */
export function readHostMemory(): HostMemoryUsage {
  const sampledAt = new Date().toISOString()
  try {
    const totalBytes = totalmem()
    const freeBytes = freemem()
    if (!Number.isSafeInteger(totalBytes) || !Number.isSafeInteger(freeBytes) || totalBytes <= 0 || freeBytes < 0 || freeBytes > totalBytes) {
      throw new Error('invalid_memory_sample')
    }
    return { available: true, source: 'electron_host', sampledAt, totalBytes, freeBytes, usedBytes: totalBytes - freeBytes }
  } catch {
    return { available: false, source: 'electron_host', sampledAt, totalBytes: null, usedBytes: null, freeBytes: null, error: 'read_failed' }
  }
}

export function createLocalMetricsReader(root: string, options: StorageScanOptions = {}): () => Promise<LocalMetricsSnapshot> {
  const readStorage = createClientStorageSampler(root, options)
  return async () => ({ storage: await readStorage(), memory: readHostMemory() })
}
