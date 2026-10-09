<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useUiStore } from '@/stores/ui'
import { useSetupStore } from '@/stores/setup'
import { useSessionStore } from '@/stores/session'
import QtIcon from '@/components/QtIcon.vue'
const ui=useUiStore()
const setup=useSetupStore()
const session=useSessionStore()
const busy=ref(false)
const noticeOpen=ref(false)
async function save(key:'notify'|'tray'|'launch',value:boolean):Promise<void>{
  if(!window.qt){message.info('此偏好需要在桌面控制台中设置');return}
  busy.value=true
  try{
    if(key==='launch'){const applied=await window.qt.app.setAutoLaunch(value);await ui.persist({app:{auto_launch:applied}});ui.autoLaunch=applied}
    else if(key==='tray'){await ui.persist({app:{minimize_to_tray_on_close:value}});ui.trayOnClose=value}
    else{await ui.persist({notify:{enabled:value}});ui.notifyEnabled=value}
    message.success('偏好已保存')
  }catch(e){message.error(e instanceof Error?e.message:String(e))}finally{busy.value=false}
}
onMounted(()=>{void setup.loadNotice()})
</script>
<template><div class="qt-page preferences"><header class="qt-page-heading"><div><span class="qt-eyebrow">MAKE IT YOURS</span><h1>偏好设置</h1><p>让工作台适合你的日常习惯。</p></div></header><div class="preferences-layout"><section class="qt-surface"><h2>日常使用</h2><div class="preference-row"><span class="preference-icon"><QtIcon name="bell"/></span><div><h3>桌面提醒</h3><p>账号需要登录或发生严重告警时通知我</p></div><a-switch aria-label="桌面提醒" :checked="ui.notifyEnabled" :loading="busy" @change="(v:any)=>save('notify',!!v)"/></div><div class="preference-row"><span class="preference-icon"><QtIcon name="screen"/></span><div><h3>关闭窗口后继续运行</h3><p>最小化到托盘，保留后台工作</p></div><a-switch aria-label="关闭窗口后继续运行" :checked="ui.trayOnClose" :loading="busy" @change="(v:any)=>save('tray',!!v)"/></div><div class="preference-row"><span class="preference-icon"><QtIcon name="refresh"/></span><div><h3>登录 Windows 后自动启动</h3><p>启动后进入托盘，不打断当前工作</p></div><a-switch aria-label="登录后自动启动" :checked="ui.autoLaunch" :loading="busy" @change="(v:any)=>save('launch',!!v)"/></div></section><aside class="qt-glass about-card"><span class="about-symbol">Q<span>.</span></span><h2>QTrade</h2><p>多通道 IM 工作台</p><div class="about-version"><span>控制台版本</span><b>{{ session.appVersion?.console??'未获取' }}</b></div><div class="about-version"><span>运行状态</span><b>{{ session.agentReachable?'已连接':'未连接' }}</b></div><a href="#/env">查看运行环境</a></aside><section class="qt-surface notice-card"><div><QtIcon name="shield"/><h2>使用告知</h2></div><p>{{ setup.acked?'已确认当前版本的使用告知':'查看本机采集与账号操作的使用说明' }}</p><a-button @click="noticeOpen=true">查看使用告知</a-button></section></div><a-modal v-model:open="noticeOpen" title="使用告知" :footer="null" :width="640"><p v-if="setup.error" class="qt-warn">{{ setup.error }}</p><div class="notice-text">{{ setup.noticeText||'告知内容尚未加载' }}</div></a-modal></div></template>
<style scoped>
.preferences-layout{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:24px;margin-top:30px;max-width:1160px}.qt-surface h2{font-size:17px;font-weight:600;margin:4px 0 20px}.preference-row{display:flex;align-items:center;gap:18px;padding:25px 0;border-top:1px solid #f0eaf5}.preference-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:#f3edf9;color:#a586bb;flex-shrink:0}.preference-row>div{flex:1}.preference-row h3{font-size:14px;font-weight:500;margin:0 0 8px}.preference-row p{font-size:12px;color:#82728e;margin:0}.about-card{padding:30px;text-align:center;background:linear-gradient(135deg,#f2eafa99,#ffffff90,#fff2d8a0)}.about-symbol{font-size:56px;color:#7547a8;font-weight:700;letter-spacing:-5px}.about-symbol span{color:#e9af43}.about-card h2{font-size:22px;margin:6px 0}.about-card>p{font-size:12px;color:#9c89ac;margin-bottom:30px}.about-version{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:#a392b1;margin:16px 0}.about-version b{font-weight:400;color:#7d628e}.about-card>a{display:block;margin-top:25px;font-size:12px}.notice-card>div{display:flex;align-items:center;gap:10px;color:#8e72a6}.notice-card h2{margin:0}.notice-card>p{font-size:13px;color:#82728e;margin:18px 0}.notice-text{white-space:pre-wrap;font-size:14px;line-height:1.9;max-height:65vh;overflow:auto}@media(max-width:800px){.preferences-layout{grid-template-columns:1fr}.about-card{grid-row:3}.preference-row{gap:10px}.preference-icon{display:none}}
</style>
