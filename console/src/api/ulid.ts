/** ULID(00 §6):26 字符 Crockford Base32,时间有序。trace_id / idempotency_key 用。 */

const ENCODING = '0123456789ABCDEFGHJKMNPQRSTVWXYZ' // Crockford base32,去掉 I L O U
const TIME_LEN = 10
const RANDOM_LEN = 16

function randomBytes(n: number): Uint8Array {
  const buf = new Uint8Array(n)
  const g = globalThis.crypto
  if (g && typeof g.getRandomValues === 'function') {
    g.getRandomValues(buf)
  } else {
    for (let i = 0; i < n; i++) buf[i] = Math.floor(Math.random() * 256)
  }
  return buf
}

function encodeTime(now: number): string {
  let out = ''
  let t = now
  for (let i = TIME_LEN - 1; i >= 0; i--) {
    out = ENCODING[t % 32] + out
    t = Math.floor(t / 32)
  }
  return out
}

function encodeRandom(): string {
  const bytes = randomBytes(RANDOM_LEN)
  let out = ''
  for (let i = 0; i < RANDOM_LEN; i++) out += ENCODING[bytes[i] % 32]
  return out
}

export function ulid(now: number = Date.now()): string {
  return encodeTime(now) + encodeRandom()
}

/** 登录尝试 id(00 §6:`ls_` + ULID);控制台只在需要回传时用 */
export function loginSessionId(): string {
  return `ls_${ulid()}`
}
