/**
 * `#88`/`#89` 各设置组的**可提交键白名单** —— 唯一出处 `docs/07-配置项总表.md` §2(agent.toml),
 * 与后端 `_known_setting_keys()`(= 各配置 dataclass 的字段名)逐字对齐。
 *
 * 为什么单独成文件而不是写在 `SetPage.vue` 里:P-2 那个缺陷(保留期卡片提交 `text_days`/`raw_enabled`,
 * 07 `[retention]` 根本没有这两个键 ⇒ 用户改「消息正文保留天数」等于没改,还把同组其余键洗回默认)
 * 的根因就是**键名没有一个可对账的落点**。放在这里,`tests/unit/settings-keys.spec.ts` 才能把它
 * 与 mock(按 07 写的那份默认值)双向对账:任何一侧漂了,单测先红。
 *
 * 🔴 `#89` 是**整组替换(缺省键回默认)**,且对未知键回 **`400 INVALID_ARGS`**
 * (总控 2026-09-21 裁决,独立联调 P-3)。一个野键既存不进去、又让整组保存失败,
 * 所以下发前一律按这里的白名单过滤。
 */

/** 07 §2 `[retention]`(全部 ≤30/E-18);`disk_low_watermark_mb` 是 `disk_high_mb` 的只读别名,不可提交 */
export const RETENTION_KEYS = [
  'files_days', 'messages_days', 'media_days', 'raw_days', 'mail_archive_days', 'commands_days',
  'audit_days', 'idempotency_days', 'mail_inbox_rows_days', 'export_jobs_days',
  'health_raw_h', 'health_1m_d', 'health_1h_d',
  'cleanup_at', 'cleanup_batch', 'disk_warn_mb', 'disk_high_mb', 'disk_critical_mb',
] as const

/**
 * 07 §2 `[api]` ∩ 后端 `ApiConfig`;`public_domain` 不在 `agent.toml` 里,
 * 唯一存放处是 `settings`(02 #102),但它**在**本组的可提交键里。
 * ⚠️ 07 另有 `https_port`/`tls_cert`/`tls_key`/`hmac_clock_skew_s`/`nonce_ttl_s`,
 * 后端 `ApiConfig` 尚未落这些字段 ⇒ 提交会被未知键判定挡回 400,故暂不列入(已转文档方/后端)。
 */
export const API_KEYS = [
  'bind', 'port', 'ws_impl', 'rate_default_per_min', 'http_sync_max_wait_ms',
  'unauth_health_sources', 'api_version', 'public_ip_check_interval_s', 'public_ip_probe_urls',
  'public_domain',
] as const

/**
 * 07 §2 `[pool]` ∩ 后端 `PoolConfig`。
 * 🔴 内存水位阈值 `mem_warn_mb`/`mem_critical_mb` 在**这一组**,不在 `resources` 组
 * (01 §4 把这两个控件写成 `PUT /settings/resources`,与 07 冲突,已转文档方订正)。
 */
export const POOL_KEYS = [
  'wsl_reserved_mb', 'windows_reserved_mb', 'quota_qidian_mb', 'quota_qq_mb', 'quota_wechat_mb',
  'autocalibrate_on_first_login', 'mem_warn_mb', 'mem_critical_mb',
] as const

/** 07 §2 `[ocr]`;`engine` 只读(不可选、不可填端点,A-1),故不在可提交集里 */
export const OCR_KEYS = ['model_dir', 'min_conf', 'lang'] as const

/**
 * 07 §2 `[asr]`。🔴 密钥**不在这里** —— 07 登记的是 `api_key_ref`(保险库引用,只读回);
 * 写入走 #88 的「密码类只写不读」通道:键名以 `secret`/`password`/`token` 结尾才会被后端
 * `_stash_secrets()` 收进 Vault 并回 `*_ref`,故下发键 = `api_key_secret`(见 `ASR_SECRET_KEY`)。
 */
export const ASR_KEYS = ['provider', 'endpoint', 'model', 'concurrency', 'min_confidence', 'prefer_native'] as const

/** ASR 密钥的**写入**键名(只写不读);读回的是 `api_key_ref` */
export const ASR_SECRET_KEY = 'api_key_secret'

/** 只留白名单里的键(值为 `undefined` 的不下发) */
export function pickKeys(src: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const k of keys) if (src[k] !== undefined) out[k] = src[k]
  return out
}
