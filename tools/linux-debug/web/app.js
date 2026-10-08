const $ = (id) => document.getElementById(id);
let csrf = '', busy = false, imageUrl = null;
let liveVideo=null;
let connection = {base: '', token: '', version: 0};
let controller = new AbortController();

function feedback(message, error=false) {
  $('feedback').textContent=message;
  $('feedback').className=error?'error':'';
}
function unavailable(message) {
  liveVideo?.stop();
  csrf='';
  $('badge').textContent='未连接调试服务'; $('badge').className='';
  $('diagnostic').textContent=message;
  document.querySelectorAll('[data-key],#connect,#launch,#screenshot,#interactive,#text,#send-text,#live-start').forEach(x=>x.disabled=true);
  ['android','boot','installed','running','checked'].forEach(id=>$(id).textContent='—');
  $('foreground').textContent=''; $('interactive').checked=false;
  $('screen').hidden=true; $('empty').hidden=false; $('screen-status').textContent='等待连接';
  $('text').value='';
  if(imageUrl) URL.revokeObjectURL(imageUrl);
  imageUrl=null; $('screen').removeAttribute('src');
}
function selectConnection(base, token) {
  controller.abort(); controller=new AbortController();
  connection={base,token,version:connection.version+1};
  busy=false; document.body.classList.remove('busy');
  unavailable('等待验证后端与设备连接。');
  $('connection-status').textContent=base?`后端：${base}`:'尚未选择后端';
}
async function request(path, body, screenshot=false) {
  if (!connection.base) throw new Error('请先填写云端后端地址并连接。');
  const version=connection.version;
  const headers={};
  if(connection.token) headers.Authorization=`Bearer ${connection.token}`;
  if(body!==undefined){headers['Content-Type']='application/json';headers['X-Debug-CSRF']=csrf;}
  let response;
  try {
    response=await fetch(connection.base+'/api/debug/'+path, {
      method:body===undefined?'GET':'POST', headers,
      body:body===undefined?undefined:JSON.stringify(body),
      credentials:'omit', redirect:'error', cache:'no-store',
      signal:AbortSignal.any([controller.signal,AbortSignal.timeout(path==='qidian/launch'?150000:60000)])
    });
  } catch(error) {
    if(version!==connection.version) throw new DOMException('连接已切换','AbortError');
    unavailable('无法连接后端。请检查 HTTPS 地址、服务状态和允许访问的站点来源。');
    throw new Error('连接失败或超时；当前未取得有效设备状态。');
  }
  const json=response.headers.get('content-type')?.includes('application/json');
  const result=screenshot&&response.ok?await response.blob():json?await response.json():null;
  if(version!==connection.version) throw new DOMException('连接已切换','AbortError');
  if(!response.ok){
    const message=response.status===401?'访问凭据无效，请重新输入。':result?.error||`后端返回 HTTP ${response.status}`;
    if([401,403,502,503,504].includes(response.status)) unavailable(message);
    throw new Error(message);
  }
  if(screenshot && !result.type.startsWith('image/png')) throw new Error('后端未返回有效设备截图。');
  if(!screenshot && !json) throw new Error('该地址未提供调试 API，请检查后端地址。');
  return result;
}
const api=(path,body)=>request(path,body);
async function act(action) {
  if(busy)return;
  const version=connection.version;
  busy=true; document.body.classList.add('busy');
  try {await action();} catch(error) {if(version===connection.version)feedback(error.message,true);}
  finally {if(version===connection.version){busy=false;document.body.classList.remove('busy');}}
}
async function refresh() {
  const version=connection.version;
  $('badge').textContent='检查中';
  let status;
  try {
    status=await api('status');
    if(!status.device || !status.csrf) throw new Error('无效的调试服务响应');
  } catch(error) {
    if(version===connection.version) unavailable(error.message);
    throw error;
  }
  const d=status.device; csrf=status.csrf;
  const ready=d.connected&&d.booted&&!d.error;
  $('connect').disabled=false;
  $('badge').textContent=ready?'设备就绪':d.error?'设备异常':d.connected?'启动中':'未连接';
  $('badge').className=ready?'ready':'pending';
  $('android').textContent=d.android?`${d.android} · ${d.abi}`:'—';
  $('boot').textContent=d.booted?'已完成':d.connected?'启动中':'等待设备';
  $('installed').textContent=d.qidian_installed?'已安装':'未检测到';
  $('running').textContent=d.qidian_running?'运行中':'未运行';
  $('checked').textContent=new Date(status.checked_at*1000).toLocaleTimeString('zh-CN');
  $('foreground').textContent=d.foreground;
  $('diagnostic').textContent=d.error || (ready?(d.qidian_installed?'可打开企点并查看登录页面。':'Android 已就绪，等待安装企点 APK。'):'等待 Android 完成启动。');
  document.querySelectorAll('[data-key],#screenshot,#interactive,#text,#send-text,#live-start').forEach(x=>x.disabled=!ready);
  $('launch').disabled=!(ready&&d.qidian_installed);
  if(!ready){$('screen').hidden=true;$('empty').hidden=false;$('screen-status').textContent='等待连接';}
  return status;
}
async function screenshot() {
  liveVideo?.stop();
  const blob=await request('screenshot',undefined,true);
  if(imageUrl)URL.revokeObjectURL(imageUrl);
  imageUrl=URL.createObjectURL(blob); $('screen').src=imageUrl; $('screen').hidden=false; $('empty').hidden=true;
  $('screen-status').textContent=new Date().toLocaleTimeString('zh-CN');
}
$('connection-form').onsubmit=(event)=>{
  event.preventDefault(); if(busy)return;
  try {
    const url=new URL($('backend-url').value.trim());
    const loopback=host=>['localhost','127.0.0.1'].includes(host);
    const localTest=loopback(location.hostname)&&loopback(url.hostname)&&url.protocol==='http:';
    if((url.protocol!=='https:'&&!localTest)||url.username||url.password||url.search||url.hash||url.pathname!=='/')
      throw new Error('请填写后端的 HTTPS 根地址，不包含路径或凭据。');
    const token=$('backend-token').value.trim();
    if(url.origin!==location.origin&&!token) throw new Error('连接远程后端需要访问凭据。');
    $('backend-token').value='';
    selectConnection(url.origin,token);
    act(async()=>{await refresh();feedback('已验证后端连接，设备状态来自真实云端服务。');});
  } catch(error){feedback(error.message,true);}
};
$('disconnect').onclick=()=>{
  selectConnection('',''); $('backend-token').value='';
  feedback('已断开连接并清除本页面中的访问凭据。');
};
$('refresh').onclick=()=>act(async()=>{await refresh();feedback('状态已更新');});
$('connect').onclick=()=>act(async()=>{await api('connect',{});await refresh();feedback('ADB 已连接');});
$('launch').onclick=()=>act(async()=>{await api('qidian/launch',{});await refresh();if(!liveVideo.active)await screenshot();feedback('企点已启动，请在画面中确认登录状态');});
$('screenshot').onclick=()=>act(screenshot);
document.querySelectorAll('[data-key]').forEach(button=>button.onclick=()=>act(async()=>{if(liveVideo.ready)liveVideo.send({type:'key',key:button.dataset.key});else{await api('key/'+button.dataset.key,{});await screenshot();}}));
$('screen').onclick=(event)=>{
  if(!$('interactive').checked)return;
  const rect=$('screen').getBoundingClientRect();
  const x=Math.max(0,Math.min($('screen').naturalWidth-1,Math.floor((event.clientX-rect.left)*$('screen').naturalWidth/rect.width)));
  const y=Math.max(0,Math.min($('screen').naturalHeight-1,Math.floor((event.clientY-rect.top)*$('screen').naturalHeight/rect.height)));
  act(async()=>{await api('tap',{x,y});await screenshot();});
};
$('text-form').onsubmit=(event)=>{event.preventDefault();const text=$('text').value;if(!text)return;act(async()=>{if(liveVideo.ready)liveVideo.send({type:'text',text});else await api('text',{text});$('text').value='';feedback('已输入到设备');});};
liveVideo=new window.QTradeVideo($('video-screen'),(message,ready)=>{
  $('video-state').textContent=message;$('live-stop').disabled=!liveVideo?.active;
  $('select-all').disabled=!ready;
  if(ready){$('screen').hidden=true;$('empty').hidden=true;$('screen-status').textContent='实时 H.264';}
  else if($('screen').hidden){$('empty').hidden=false;$('screen-status').textContent='等待画面';}
});
$('live-start').onclick=()=>{try{liveVideo.start(connection.base,connection.token,csrf);$('live-stop').disabled=false;}catch(error){feedback(error.message,true);}};
$('live-stop').onclick=()=>liveVideo.stop();
$('select-all').onclick=()=>{try{liveVideo.send({type:'key',key:'select_all'});}catch(error){feedback(error.message,true);}};
document.addEventListener('visibilitychange',()=>{if(document.hidden)liveVideo.stop('页面已隐藏，实时流已暂停');});
window.addEventListener('pagehide',()=>{controller.abort();liveVideo.stop();connection.token='';});
if(['localhost','127.0.0.1'].includes(location.hostname)) {
  $('backend-url').value=location.origin; selectConnection(location.origin,''); act(refresh);
} else {
  unavailable('请输入云端调试后端的 HTTPS 地址和访问凭据，连接后启用设备操作。');
}
