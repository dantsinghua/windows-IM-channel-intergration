/** 本机只读指标；数值来源与业务 Agent 监控分开。 */
export type StorageSampleError = 'timeout' | 'limit_exceeded' | 'read_failed' | 'invalid_root'

interface StorageSampleBase {
  scope: 'client_directory'
  /** 普通文件长度总和，不代表卷容量或压缩后的分配空间。 */
  measurement: 'file_sizes'
  sampledAt: string
  excludedLinks: number
}

export type ClientStorageUsage = StorageSampleBase & (
  | { available: true; bytes: number; error?: never }
  | { available: false; bytes: null; error: StorageSampleError }
)

export type HostMemoryUsage = {
  source: 'electron_host'
  sampledAt: string
} & (
  | { available: true; totalBytes: number; usedBytes: number; freeBytes: number; error?: never }
  | { available: false; totalBytes: null; usedBytes: null; freeBytes: null; error: 'read_failed' }
)

export interface LocalMetricsSnapshot {
  memory: HostMemoryUsage
  storage: ClientStorageUsage
}
