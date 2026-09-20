import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'
import vueParser from 'vue-eslint-parser'

export default [
  { ignores: ['dist/**', 'release/**', 'node_modules/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: { parser: tseslint.parser, ecmaVersion: 2022, sourceType: 'module' },
    },
  },
  {
    files: ['src/**/*.{ts,vue}', 'tests/**/*.ts'],
    languageOptions: {
      globals: {
        window: 'readonly', document: 'readonly', navigator: 'readonly', location: 'readonly',
        fetch: 'readonly', Response: 'readonly', Request: 'readonly', Headers: 'readonly',
        URL: 'readonly', URLSearchParams: 'readonly', Blob: 'readonly', File: 'readonly',
        FormData: 'readonly', AbortSignal: 'readonly', AbortController: 'readonly',
        WebSocket: 'readonly', BodyInit: 'readonly', RequestInfo: 'readonly', RequestInit: 'readonly',
        ResponseInit: 'readonly', HTMLElement: 'readonly', HTMLCanvasElement: 'readonly',
        PointerEvent: 'readonly', KeyboardEvent: 'readonly', WheelEvent: 'readonly',
        VideoDecoder: 'readonly', VideoFrame: 'readonly', EncodedVideoChunk: 'readonly',
        Uint8Array: 'readonly', ArrayBuffer: 'readonly', DataView: 'readonly',
        setTimeout: 'readonly', clearTimeout: 'readonly', setInterval: 'readonly', clearInterval: 'readonly',
        console: 'readonly', globalThis: 'readonly', crypto: 'readonly', Buffer: 'readonly',
        process: 'readonly', __dirname: 'readonly',
      },
    },
  },
  {
    files: ['**/*.{ts,vue}'],
    rules: {
      'vue/multi-word-component-names': 'off',
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/attributes-order': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/first-attribute-linebreak': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
      // 只是排版噪音,不影响正确性
      'vue/multiline-html-element-content-newline': 'off',
      'vue/attribute-hyphenation': 'off',
      'vue/v-on-event-hyphenation': 'off',
      'vue/one-component-per-file': 'off',
      // 检测控制字符本身就要写控制字符(R6-48 的 clean_text 口径)
      'no-control-regex': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    files: ['mock/**/*.mjs', 'scripts/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { process: 'readonly', console: 'readonly', URL: 'readonly', setTimeout: 'readonly', setInterval: 'readonly', clearInterval: 'readonly', Buffer: 'readonly' },
    },
    // mock 要按 R6-48 的口径识别控制字符,写控制字符是本意
    rules: { 'no-control-regex': 'off' },
  },
  {
    files: ['electron/**/*.ts'],
    languageOptions: { globals: { console: 'readonly', process: 'readonly', setTimeout: 'readonly', setInterval: 'readonly', clearTimeout: 'readonly', Buffer: 'readonly', __dirname: 'readonly' } },
  },
]
