<script setup lang="ts">
/** 由 JSON Schema 生成参数表单(01 §2.7.5);string/number/boolean/enum/object → 输入框/数字/开关/下拉/JSON 编辑器 */
import { computed } from 'vue'
import type { JsonSchema } from '@/api/types'
import { cmd as T } from '@/testids'

const props = defineProps<{
  schema?: JsonSchema
  modelValue: Record<string, unknown>
  /** 按 error.details[].pointer 标红的字段 */
  invalidFields?: string[]
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: Record<string, unknown>): void; (e: 'pick-session', key: string): void }>()

interface Field { key: string; label: string; required: boolean; schema: JsonSchema }

const fields = computed<Field[]>(() => {
  const props_ = props.schema?.properties ?? {}
  const req = new Set(props.schema?.required ?? [])
  return Object.entries(props_).map(([key, s]) => ({
    key,
    label: s.title ?? key,
    required: req.has(key),
    schema: s,
  }))
})

function set(key: string, v: unknown): void {
  emit('update:modelValue', { ...props.modelValue, [key]: v })
}

function kind(s: JsonSchema): 'enum' | 'boolean' | 'number' | 'object' | 'string' {
  if (s.enum?.length) return 'enum'
  if (s.type === 'boolean') return 'boolean'
  if (s.type === 'number' || s.type === 'integer') return 'number'
  if (s.type === 'object' || s.type === 'array') return 'object'
  return 'string'
}

function isInvalid(key: string): boolean {
  return (props.invalidFields ?? []).includes(key)
}

function jsonText(v: unknown): string {
  try { return JSON.stringify(v ?? {}, null, 2) } catch { return '{}' }
}

function setJson(key: string, text: string): void {
  try { set(key, JSON.parse(text)) } catch { /* 输入中途不合法就先不写回 */ }
}
</script>

<template>
  <a-form layout="vertical" :data-testid="T.form">
    <a-form-item
      v-for="f in fields"
      :key="f.key"
      :label="f.label + (f.required ? ' *' : '')"
      :validate-status="isInvalid(f.key) ? 'error' : undefined"
      :help="isInvalid(f.key) ? '请检查该字段' : f.schema.description"
    >
      <div class="qt-row">
        <a-select
          v-if="kind(f.schema) === 'enum'"
          class="qt-grow"
          :data-testid="T.field(f.key)"
          :value="modelValue[f.key]"
          :options="(f.schema.enum ?? []).map((v) => ({ value: v, label: String(v) }))"
          @change="(v: unknown) => set(f.key, v)"
        />
        <a-switch
          v-else-if="kind(f.schema) === 'boolean'"
          :data-testid="T.field(f.key)"
          :checked="!!modelValue[f.key]"
          @change="(v: unknown) => set(f.key, !!v)"
        />
        <a-input-number
          v-else-if="kind(f.schema) === 'number'"
          class="qt-grow"
          :data-testid="T.field(f.key)"
          :value="modelValue[f.key] as number"
          :min="f.schema.minimum"
          :max="f.schema.maximum"
          @change="(v: unknown) => set(f.key, v)"
        />
        <a-textarea
          v-else-if="kind(f.schema) === 'object'"
          class="qt-grow qt-mono"
          :data-testid="T.field(f.key)"
          :rows="4"
          :value="jsonText(modelValue[f.key])"
          @change="(e: any) => setJson(f.key, e.target.value)"
        />
        <a-input
          v-else
          class="qt-grow"
          :data-testid="T.field(f.key)"
          :value="modelValue[f.key] as string"
          @change="(e: any) => set(f.key, e.target.value)"
        />
        <a-button
          v-if="f.key === 'session'"
          size="small"
          :data-testid="T.sessionPick"
          @click="emit('pick-session', f.key)"
        >从会话列表选</a-button>
      </div>
    </a-form-item>
    <a-empty v-if="!fields.length" description="该能力无参数" />
  </a-form>
</template>
