import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import App from './App.vue'
import { router } from './router'
import './theme/tokens.css'
import 'ant-design-vue/dist/reset.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(Antd)
app.provide('antdLocale', zhCN)
app.mount('#app')
