/** 文件对话框与导出落盘 —— 唯一能碰文件系统的地方(01 §2.1/§2.2) */
import { dialog, shell, type BrowserWindow } from 'electron'
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export async function saveAs(
  win: BrowserWindow | null,
  suggestName: string,
  data: Uint8Array | string,
): Promise<{ saved: boolean; path?: string }> {
  const r = await dialog.showSaveDialog(win ?? undefined!, { defaultPath: suggestName })
  if (r.canceled || !r.filePath) return { saved: false }
  await writeFile(r.filePath, typeof data === 'string' ? data : Buffer.from(data))
  return { saved: true, path: r.filePath }
}

/**
 * 选本地文件(发送图片/文件用)。
 * **只返回文件名与字节,不返回路径** —— 路径进渲染进程等于把用户磁盘结构交出去。
 */
export async function pickFile(
  win: BrowserWindow | null,
  accept?: string[],
): Promise<{ name: string; bytes: Uint8Array } | null> {
  const r = await dialog.showOpenDialog(win ?? undefined!, {
    properties: ['openFile'],
    filters: accept?.length ? [{ name: '可选文件', extensions: accept }] : undefined,
  })
  if (r.canceled || !r.filePaths[0]) return null
  const { readFile } = await import('node:fs/promises')
  const p = r.filePaths[0]
  const bytes = await readFile(p)
  return { name: p.split(/[\\/]/).pop() ?? 'file', bytes: new Uint8Array(bytes) }
}

export function logsDir(): string {
  const programData = process.env.PROGRAMDATA || join(process.env.HOME ?? '.', '.qtrade')
  return join(programData, 'QTrade', 'logs')
}

export async function openLogsDir(): Promise<void> {
  await shell.openPath(logsDir())
}
