import { describe, expect, it } from 'vitest'
import { displaySender } from '@/pages/msg/senderLabel'
import type { Message } from '@/api/types'

function msg(partial: Partial<Message>): Pick<Message, 'dir' | 'self' | 'sender' | 'session'> {
  return {
    dir: partial.dir || 'in',
    self: partial.self || false,
    sender: partial.sender || { id: '', name: null },
    session: partial.session || { id: 's', name: '', kind: 'private' },
  }
}

describe('历史消息发送人', () => {
  it('自己发出且库里没有名字时用登录昵称', () => {
    expect(displaySender(msg({ dir: 'out', self: true }), { self_nick: '安琳-盈米', label: '新微信' })).toBe('安琳-盈米')
  })

  it('企点、QQ、微信都一样：没有登录昵称时用账号名称', () => {
    for (const label of ['Lulu-南银理财', 'dantsinghua', '德儿塔外']) {
      expect(displaySender(msg({ dir: 'out', self: true, sender: { id: '1', name: null } }), { label })).toBe(label)
    }
  })

  it('库里已有发送人名就保持原样', () => {
    expect(displaySender(msg({ dir: 'out', self: true, sender: { id: '1', name: 'dantsinghua' } }), { label: '别的' })).toBe('dantsinghua')
  })

  it('私聊收到的没有发送人名时用会话标题', () => {
    expect(displaySender(msg({ dir: 'in', session: { id: 's', name: '安琳', kind: 'private' } }))).toBe('安琳')
  })

  it('群消息和纯号码标题不拿来冒充发送人', () => {
    expect(displaySender(msg({ dir: 'in', session: { id: 's', name: '48854136533@chatroom', kind: 'group' } }))).toBe('未知发送人')
    expect(displaySender(msg({ dir: 'in', session: { id: 's', name: '415011447', kind: 'private' } }))).toBe('未知发送人')
  })
})
