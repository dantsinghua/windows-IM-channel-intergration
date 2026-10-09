// @vitest-environment node
/** 客户端目录采集仅操作本测试创建的临时树，不访问真实运行目录。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { link, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises'
import * as filesystem from 'node:fs/promises'
import * as os from 'node:os'
import { tmpdir } from 'node:os'
import { join, resolve, sep } from 'node:path'
import { createClientStorageSampler, readHostMemory } from '../../electron/main/local-metrics'

// Vitest 的模块包装允许单个用例注入读取失败；其它调用仍是真实临时文件系统。
vi.mock('node:fs/promises', async (original) => ({ ...await original<typeof import('node:fs/promises')>() }))
vi.mock('node:os', async (original) => ({ ...await original<typeof import('node:os')>() }))

let fixture: string

beforeEach(async () => {
  fixture = await mkdtemp(join(tmpdir(), 'qtrade-client-storage-test-'))
})

afterEach(async () => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  // 路径来自 mkdtemp；清理只限本轮创建且仍在系统临时根下的精确目录。
  const absolute = resolve(fixture)
  if (!absolute.startsWith(resolve(tmpdir()) + sep) || !absolute.includes('qtrade-client-storage-test-')) {
    throw new Error('Unsafe fixture cleanup path')
  }
  await rm(absolute, { recursive: true, force: true })
})

describe('客户端文件夹占用的真实只读采样', () => {
  it('递归统计普通文件长度，忽略其它文件夹和分区容量', async () => {
    const root = join(fixture, 'client')
    await mkdir(join(root, 'resources', 'nested'), { recursive: true })
    await writeFile(join(root, 'console.exe'), Buffer.alloc(37))
    await writeFile(join(root, 'resources', 'app.asar'), Buffer.alloc(101))
    await writeFile(join(root, 'resources', 'nested', 'empty'), Buffer.alloc(0))
    await writeFile(join(fixture, 'unrelated.bin'), Buffer.alloc(4000))
    const result = await createClientStorageSampler(root)()
    expect(result).toMatchObject({ available: true, bytes: 138, scope: 'client_directory', measurement: 'file_sizes', excludedLinks: 0 })
    expect(Number.isNaN(Date.parse(result.sampledAt))).toBe(false)
    expect(JSON.stringify(result)).not.toContain(fixture)
  })

  it('空目录实测零保持可用，不当成未知', async () => {
    const result = await createClientStorageSampler(fixture)()
    expect(result).toMatchObject({ available: true, bytes: 0 })
  })

  it('同一文件的硬链接只占用一份文件空间', async () => {
    await writeFile(join(fixture, 'first.bin'), Buffer.alloc(43))
    await link(join(fixture, 'first.bin'), join(fixture, 'second.bin'))
    const result = await createClientStorageSampler(fixture)()
    expect(result).toMatchObject({ available: true, bytes: 43 })
  })

  it('不穿过外部目录、文件符号链接或循环链接', async () => {
    const root = join(fixture, 'client')
    const outside = join(fixture, 'outside')
    await mkdir(root)
    await mkdir(outside)
    await writeFile(join(root, 'owned.bin'), Buffer.alloc(29))
    await writeFile(join(outside, 'foreign.bin'), Buffer.alloc(7000))
    await symlink(outside, join(root, 'external-directory'), 'dir')
    await symlink(join(outside, 'foreign.bin'), join(root, 'external-file'), 'file')
    await symlink(root, join(root, 'loop'), 'dir')
    const result = await createClientStorageSampler(root)()
    expect(result).toMatchObject({ available: true, bytes: 29, excludedLinks: 3 })
  })

  it('根路径本身是链接时拒绝越界扫描', async () => {
    const real = join(fixture, 'real')
    await mkdir(real)
    await writeFile(join(real, 'private.bin'), Buffer.alloc(9000))
    const alias = join(fixture, 'alias')
    await symlink(real, alias, 'dir')
    expect(await createClientStorageSampler(alias)()).toMatchObject({ available: false, bytes: null, error: 'invalid_root' })
  })

  it('根路径是普通文件时不将单文件冒充客户端目录', async () => {
    const root = join(fixture, 'not-a-directory')
    await writeFile(root, Buffer.alloc(123))
    expect(await createClientStorageSampler(root)()).toMatchObject({ available: false, bytes: null, error: 'invalid_root' })
  })

  it('不存在的路径报告失败，不伪造零容量', async () => {
    const result = await createClientStorageSampler(join(fixture, 'missing'))()
    expect(result.available).toBe(false)
    expect(result.bytes).toBeNull()
    expect(result.error).toBeTruthy()
  })

  it('超过条目预算时不得把部分已扫描容量作为总量', async () => {
    await writeFile(join(fixture, 'one.bin'), Buffer.alloc(10))
    await writeFile(join(fixture, 'two.bin'), Buffer.alloc(20))
    await writeFile(join(fixture, 'three.bin'), Buffer.alloc(30))
    const result = await createClientStorageSampler(fixture, { maxEntries: 1 })()
    expect(result).toMatchObject({ available: false, bytes: null, error: 'limit_exceeded' })
  })

  it('超过递归深度时明确失败，不遗漏深层内容后返回成功', async () => {
    await mkdir(join(fixture, 'level-one', 'level-two', 'level-three'), { recursive: true })
    await writeFile(join(fixture, 'level-one', 'level-two', 'level-three', 'nested.bin'), Buffer.alloc(400))
    const result = await createClientStorageSampler(fixture, { maxDepth: 1 })()
    expect(result).toMatchObject({ available: false, bytes: null, error: 'limit_exceeded' })
  })

  it('任意文件元数据读取失败时整轮失败，不报告不完整合计', async () => {
    await writeFile(join(fixture, 'readable.bin'), Buffer.alloc(40))
    await writeFile(join(fixture, 'unreadable.bin'), Buffer.alloc(60))
    const realLstat = filesystem.lstat
    vi.spyOn(filesystem, 'lstat').mockImplementation(((path: Parameters<typeof realLstat>[0], options?: Parameters<typeof realLstat>[1]) => {
      if (String(path).endsWith('unreadable.bin')) return Promise.reject(new Error('fixture access failure'))
      return realLstat(path, options)
    }) as typeof realLstat)
    const result = await createClientStorageSampler(fixture)()
    expect(result).toMatchObject({ available: false, bytes: null, error: 'read_failed' })
    expect(JSON.stringify(result)).not.toContain(fixture)
  })

  it('慢读取超时及时返回未知，未结束时并发刷新不堆积扫描', async () => {
    await writeFile(join(fixture, 'actual.bin'), Buffer.alloc(80))
    const realRootStat = await filesystem.lstat(fixture, { bigint: true })
    let release: ((value: typeof realRootStat) => void) | undefined
    const blocked = new Promise<typeof realRootStat>((resolve) => { release = resolve })
    const stat = vi.spyOn(filesystem, 'lstat').mockImplementationOnce((() => blocked) as unknown as typeof filesystem.lstat)
    const sample = createClientStorageSampler(fixture, { timeoutMs: 10, cacheMs: 0 })
    try {
      const first = sample()
      const second = sample()
      expect(stat).toHaveBeenCalledTimes(1)
      expect(await first).toMatchObject({ available: false, bytes: null, error: 'timeout' })
      expect(await second).toMatchObject({ available: false, bytes: null, error: 'timeout' })
      expect(await sample()).toMatchObject({ available: false, bytes: null, error: 'timeout' })
      expect(stat).toHaveBeenCalledTimes(1)
    } finally {
      release!(realRootStat)
      await new Promise<void>((resolve) => setImmediate(resolve))
    }
    expect(await sample()).toMatchObject({ available: true, bytes: 80 })
  })

  it('缓存期间保持同一次采样，过期后重新读取实际文件大小', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-08T12:00:00Z'))
    const file = join(fixture, 'growing.bin')
    await writeFile(file, Buffer.alloc(50))
    const sample = createClientStorageSampler(fixture, { cacheMs: 1000 })
    const first = await sample()
    await writeFile(file, Buffer.alloc(150))
    expect(await sample()).toEqual(first)
    vi.setSystemTime(new Date('2026-10-08T12:00:02Z'))
    const second = await sample()
    expect(second).toMatchObject({ available: true, bytes: 150 })
    expect(second.sampledAt).not.toBe(first.sampledAt)
  })

  it('失败后缓存过期可重新采样，不永久保留错误', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-08T12:00:00Z'))
    const root = join(fixture, 'arrives-later')
    const sample = createClientStorageSampler(root, { cacheMs: 1000 })
    expect(await sample()).toMatchObject({ available: false, bytes: null })
    await mkdir(root)
    await writeFile(join(root, 'actual.bin'), Buffer.alloc(25))
    vi.setSystemTime(new Date('2026-10-08T12:00:02Z'))
    expect(await sample()).toMatchObject({ available: true, bytes: 25 })
  })
})

describe('宿主物理内存口径', () => {
  it('总量、剩余和占用使用宿主读数，不依赖 WSL 预算', () => {
    vi.spyOn(os, 'totalmem').mockReturnValue(32 * 1024 ** 3)
    vi.spyOn(os, 'freemem').mockReturnValue(11 * 1024 ** 3)
    expect(readHostMemory()).toMatchObject({ available: true, source: 'electron_host', totalBytes: 32 * 1024 ** 3, freeBytes: 11 * 1024 ** 3, usedBytes: 21 * 1024 ** 3 })
  })

  it('剩余为零的真实满载值有效', () => {
    vi.spyOn(os, 'totalmem').mockReturnValue(8 * 1024 ** 3)
    vi.spyOn(os, 'freemem').mockReturnValue(0)
    expect(readHostMemory()).toMatchObject({ available: true, totalBytes: 8 * 1024 ** 3, freeBytes: 0, usedBytes: 8 * 1024 ** 3 })
  })

  it.each([NaN, -1, 64 * 1024 ** 3])('非法剩余读数 %s 不生成伪造占用', (free) => {
    vi.spyOn(os, 'totalmem').mockReturnValue(8 * 1024 ** 3)
    vi.spyOn(os, 'freemem').mockReturnValue(free)
    expect(readHostMemory()).toMatchObject({ available: false, totalBytes: null, freeBytes: null, usedBytes: null, error: 'read_failed' })
  })
})
