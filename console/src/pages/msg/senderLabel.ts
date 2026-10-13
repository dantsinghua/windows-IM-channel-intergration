import type { Account, Message } from '@/api/types'

function rawPeerTitle(title: string): boolean {
  if (title.endsWith('@chatroom') || title.endsWith('@openim') || title.startsWith('wxid_')) return true
  return /^\d{5,}$/.test(title)
}

/** 自己发出的用本账号昵称或名称；私聊收到且库里没有发送人名时用会话标题。 */
export function displaySender(
  message: Pick<Message, 'dir' | 'self' | 'sender' | 'session'>,
  account?: Pick<Account, 'self_nick' | 'label'> | null,
): string {
  const stored = (message.sender?.name || '').trim()
  if (stored) return stored
  const isSelf = message.self || message.dir === 'out'
  if (isSelf) {
    const nick = (account?.self_nick || '').trim() || (account?.label || '').trim()
    if (nick) return nick
  } else if (message.session?.kind === 'private') {
    const title = (message.session.name || '').trim()
    if (title && !rawPeerTitle(title)) return title
  }
  return '未知发送人'
}
