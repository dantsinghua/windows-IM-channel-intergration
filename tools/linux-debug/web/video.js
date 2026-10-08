// qtrade-scrcpy-v1: JSON codec header, then uint64 BE PTS(ms) + Annex-B H.264.
function qtradeNals(data) {
  const starts=[];
  for(let i=0;i<data.length-3;i++){
    if(data[i]===0&&data[i+1]===0){
      const size=data[i+2]===1?3:data[i+2]===0&&data[i+3]===1?4:0;
      if(size){starts.push([i,i+size]);i+=size-1;}
    }
  }
  return starts.map((entry,i)=>data.subarray(entry[1],starts[i+1]?.[0]??data.length));
}
class QTradeVideo {
  constructor(canvas, onState) {
    this.canvas=canvas; this.onState=onState; this.ws=null; this.decoder=null;
    this.frames=0; this.codec=''; this.needsKey=true; this.pointer=null;
    canvas.addEventListener('pointerdown',e=>{
      if(!document.getElementById('interactive').checked||!this.ready||this.pointer!==null)return;
      e.preventDefault();this.pointer=e.pointerId;canvas.setPointerCapture(e.pointerId);
      this.touch('down',e);
    });
    canvas.addEventListener('pointermove',e=>{
      if(this.pointer!==e.pointerId)return;
      const now=performance.now();if(now-(this.lastMove||0)<30)return;
      this.lastMove=now;this.touch('move',e);
    });
    for(const [event,action] of [['pointerup','up'],['pointercancel','cancel']])
      canvas.addEventListener(event,e=>{if(this.pointer===e.pointerId){this.touch(action,e);this.pointer=null;}});
    document.getElementById('interactive').addEventListener('change',()=>{
      if(this.pointer!==null){this.send({type:'touch',action:'cancel',x:0,y:0});this.pointer=null;}
    });
  }
  get ready(){return this.ws?.readyState===WebSocket.OPEN&&this.frames>0;}
  get active(){return this.ws!==null;}
  touch(action,event){
    const rect=this.canvas.getBoundingClientRect();
    const x=Math.max(0,Math.min(this.canvas.width-1,Math.floor((event.clientX-rect.left)*this.canvas.width/rect.width)));
    const y=Math.max(0,Math.min(this.canvas.height-1,Math.floor((event.clientY-rect.top)*this.canvas.height/rect.height)));
    this.send({type:'touch',action,x,y});
  }
  send(message){
    if(!this.ready)throw new Error('实时控制通道尚未就绪');
    if(this.ws.bufferedAmount>65536){this.stop('控制连接拥塞，请重新开启实时画面');return;}
    this.ws.send(JSON.stringify(message));
  }
  stop(message='实时画面已停止'){
    clearTimeout(this.startTimer);clearInterval(this.statsTimer);
    if(this.ws){this.ws.onopen=this.ws.onmessage=this.ws.onerror=this.ws.onclose=null;this.ws.close();this.ws=null;}
    if(this.decoder&&this.decoder.state!=='closed')this.decoder.close();
    this.decoder=null;this.canvas.hidden=true;this.pointer=null;this.onState(message,false);
  }
  start(base,token,csrf){
    this.stop();
    if(!window.isSecureContext||!window.VideoDecoder)throw new Error('当前浏览器不支持 WebCodecs；请用新版 Chrome/Edge 通过 HTTPS 访问。');
    this.frames=0;this.codec='';this.needsKey=true;this.canvas.dataset.frames='0';
    const url=new URL('/api/debug/stream',base);url.protocol=url.protocol==='https:'?'wss:':'ws:';
    const socket=new WebSocket(url,'qtrade-scrcpy-v1');this.ws=socket;socket.binaryType='arraybuffer';
    this.onState('正在连接实时画面…',false);
    this.startTimer=setTimeout(()=>this.stop('编码器未及时返回画面，请重试'),45000);
    this.decoder=new VideoDecoder({
      output:frame=>{
        try{
          if(this.ws!==socket)return;
          if(this.canvas.width!==frame.displayWidth||this.canvas.height!==frame.displayHeight){this.canvas.width=frame.displayWidth;this.canvas.height=frame.displayHeight;}
          this.canvas.getContext('2d',{alpha:false}).drawImage(frame,0,0);
          this.canvas.hidden=false;this.frames++;this.canvas.dataset.frames=String(this.frames);
          if(this.frames===1){clearTimeout(this.startTimer);this.onState('实时视频已连接',true);}
        }finally{frame.close();}
      },
      error:()=>this.stop('视频解码失败，请停止后重试')
    });
    socket.onopen=()=>socket.send(JSON.stringify({type:'auth',token,csrf}));
    socket.onerror=()=>this.stop('实时连接失败，请检查 WSS 转发与访问凭据');
    socket.onclose=event=>this.stop(event.code===1008?'视频鉴权失败，请重新连接后端':event.code===1013?'设备已有实时连接，请先关闭原连接':'实时连接已断开');
    socket.onmessage=event=>{
      if(this.ws!==socket)return;
      try{
        if(typeof event.data==='string'){
          const meta=JSON.parse(event.data);
          if(meta.type==='error'){this.stop(meta.error);return;}
          if(meta.codec!=='h264')throw new Error('不支持的视频格式');
          this.canvas.width=meta.width;this.canvas.height=meta.height;return;
        }
        const buffer=event.data;if(buffer.byteLength<9)throw new Error('视频帧不完整');
        const timestamp=Number(new DataView(buffer).getBigUint64(0))*1000;
        const data=new Uint8Array(buffer,8);const nals=qtradeNals(data);
        const sps=nals.find(n=>(n[0]&31)===7);
        if(sps){
          if(sps.length<4)throw new Error('无效 SPS');
          const codec='avc1.'+Array.from(sps.subarray(1,4)).map(n=>n.toString(16).padStart(2,'0')).join('');
          if(codec!==this.codec){this.codec=codec;this.configure();}
        }
        const key=nals.some(n=>(n[0]&31)===5);
        if(!nals.some(n=>[1,5].includes(n[0]&31)))return;
        if(!this.codec)return;
        if(this.decoder.decodeQueueSize>4){this.decoder.reset();this.configure();}
        if(this.needsKey&&!key)return;
        this.needsKey=false;
        this.decoder.decode(new EncodedVideoChunk({type:key?'key':'delta',timestamp,data}));
      }catch(error){this.stop('视频格式或解码器异常，请重试');}
    };
    let previous=0;
    this.statsTimer=setInterval(()=>{
      if(this.ws!==socket)return;
      if(this.frames){this.onState(`实时 · ${this.frames-previous} fps · 已解码 ${this.frames} 帧`,true);previous=this.frames;}
    },1000);
  }
  configure(){
    this.decoder.configure({codec:this.codec,optimizeForLatency:true});this.needsKey=true;
  }
}
window.QTradeVideo=QTradeVideo;
