/**
 * 元泉智影上传助手 — Phase 2B-2
 * 多文件串行：scan → probe → task/init → 逐文件(transcode→presign→PUT) → notify
 */

const CK = "openclaw_uploader_config";
function lc() { try { return JSON.parse(localStorage.getItem(CK)) || {}; } catch { return {}; } }
function sc(p) { const c = { ...lc(), ...p }; localStorage.setItem(CK, JSON.stringify(c)); return c; }

async function invoke(cmd, args) {
  if (window.__TAURI__?.core) return window.__TAURI__.core.invoke(cmd, args);
  return { success: false, error: "浏览器模式" };
}

const S = {
  serverUrl: "", healthStatus: "wait", healthDetail: "",
  folderPath: "", scriptText: "", videoTheme: "",
  ffmpegInfo: null, ffprobeInfo: null,
  scanResult: null,
  batchResult: null, batchRunning: false,
};

function render() {
  document.getElementById("app").innerHTML = `
    <h1>✂️ 元泉智影上传助手</h1>
    <p class="subtitle">macOS 素材转码 / 上传客户端 · Phase 2B-2 · 多文件串行</p>

    <div class="section">
      <div class="section-title">🔗 服务器</div>
      <div class="row"><input type="text" id="i-url" value="${e(S.serverUrl)}" placeholder="http://47.93.194.154:8088" /><button onclick="doHealth()">测试</button></div>
      <div style="margin-top:6px">${badge(S.healthStatus, S.healthDetail)}</div>
    </div>

    <div class="section">
      <div class="section-title">🎬 FFmpeg</div>
      <button onclick="doFfmpeg()">检测</button>
      ${ffmpegHtml()}
    </div>

    <div class="section">
      <div class="section-title">📝 视频主题 / 新闻事件</div>
      <input type="text" id="i-theme" value="${e(S.videoTheme)}" placeholder="视频主题" style="margin-bottom:8px;width:100%" />
      <textarea id="i-script" placeholder="新闻事件摘要…">${e(S.scriptText)}</textarea>
    </div>

    <div class="section">
      <div class="section-title">📁 素材目录</div>
      <div class="row" style="gap:12px">
        <button onclick="doSelectFolder()">选择文件夹</button>
        <button onclick="doBatch()" class="primary" ${!canBatch()?'disabled':''}>${S.batchRunning?'处理中…':'开始转码上传'}</button>
      </div>
      ${S.folderPath?`<div class="folder-path">${e(S.folderPath)}</div>`:''}
      ${scanHtml()}
    </div>

    ${batchHtml()}

    <div class="footer">OpenClaw Uploader v0.4.0-alpha · Phase 2B-2 · 多文件串行转码上传</div>
  `;
  bind();
}

function e(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function badge(s,d){if(s==='ok')return`<span class="status ok">✅ 已连接${d?' · '+e(d):''}</span>`;if(s==='fail')return`<span class="status fail">❌ 失败${d?' · '+e(d):''}</span>`;return'<span class="status wait">⏳ 未测试</span>'}
function canBatch(){return S.folderPath && S.healthStatus==='ok' && !S.batchRunning && S.ffmpegInfo?.available}

function ffmpegHtml(){
  if(!S.ffmpegInfo)return'<div style="margin-top:6px;font-size:12px;color:var(--text-sec)">点击检测</div>';
  const f=S.ffmpegInfo,p=S.ffprobeInfo;
  return`<div style="margin-top:8px;font-size:12px"><div>${f.available?'<span class="status ok">✅ ffmpeg</span>':'<span class="status fail">❌ ffmpeg</span>'} <span style="color:var(--text-sec);margin-left:6px">${e(f.available?f.version:f.error)}</span></div><div style="margin-top:4px">${p.available?'<span class="status ok">✅ ffprobe</span>':'<span class="status fail">❌ ffprobe</span>'} <span style="color:var(--text-sec);margin-left:6px">${e(p.available?p.version:p.error)}</span></div></div>`;
}

function scanHtml(){
  const r=S.scanResult;if(!r)return'';
  return`<div style="margin-top:8px;font-size:12px;color:var(--text-sec)">发现 <b style="color:var(--text)">${r.total}</b> 个视频${r.skipped_hidden?' · '+r.skipped_hidden+' 隐藏跳过':''}${r.skipped_small?' · '+r.skipped_small+' 过小跳过':''}</div>`;
}

function batchHtml(){
  const r=S.batchResult;if(!r)return'';
  let h=`<div class="section"><div class="section-title">📊 处理结果</div>`;
  // 汇总
  const sumBg=r.overall_success?'rgba(16,185,129,0.1)':'rgba(245,158,11,0.1)';
  const sumBd=r.overall_success?'rgba(16,185,129,0.3)':'rgba(245,158,11,0.3)';
  h+=`<div style="background:${sumBg};border:1px solid ${sumBd};border-radius:6px;padding:12px;font-size:13px;margin-bottom:12px">`;
  h+=`<div style="font-weight:600;margin-bottom:4px">${r.overall_success?'✅ 全部成功':'⚠️ 部分完成'}</div>`;
  h+=`<div style="color:var(--text-sec)">总计 ${r.total} · 上传 ${r.uploaded} · 跳过 ${r.skipped} · 失败 ${r.failed}</div>`;
  if(r.task_id)h+=`<div style="color:var(--text-sec);margin-top:4px">task_id：${e(r.task_id)}</div>`;
  if(r.notify_ok)h+=`<div style="color:var(--text-sec)">notify：${e(r.notify_status)}</div>`;
  if(r.task_url&&r.notify_ok)h+=`<div style="margin-top:8px"><button onclick="openUrl('${e(r.task_url)}')" class="primary" style="font-size:12px;padding:5px 12px">🌐 打开 Web 工作台</button></div>`;
  h+=`</div>`;
  // 文件列表
  h+=`<div style="font-size:12px">`;
  for(const f of r.files){
    const icon=f.status==='uploaded'?'✅':f.status.startsWith('skipped')?'⏭️':'❌';
    h+=`<div style="padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center">`;
    h+=`<span>${icon}</span><span style="font-weight:500;min-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e(f.filename)}</span>`;
    h+=`<span style="color:var(--text-sec);font-size:11px">${e(f.status)}`;
    if(f.transcode_ok)h+=` · 转码${f.transcode_time.toFixed(1)}s`;
    if(f.upload_ok)h+=` · PUT 200`;
    if(f.object_key)h+=` · ${e(f.object_key.split('/').pop())}`;
    if(f.error)h+=` <span style="color:var(--danger)">${e(f.error.substring(0,80))}</span>`;
    h+=`</span></div>`;
  }
  h+=`</div></div>`;
  return h;
}

// ============================================================
async function doHealth(){
  const url=S.serverUrl||document.getElementById('i-url')?.value?.trim();
  if(!url){S.healthStatus='fail';S.healthDetail='请输入地址';render();return}
  S.serverUrl=url;sc({serverUrl:url});S.healthStatus='wait';S.healthDetail='连接中…';render();
  try{const r=await fetch(url.replace(/\/+$/,'')+'/api/health',{signal:AbortSignal.timeout(10000)});if(r.ok){const d=await r.json();S.healthStatus='ok';S.healthDetail=d.version||'ok'}else{S.healthStatus='fail';S.healthDetail='HTTP '+r.status}}catch(e){S.healthStatus='fail';S.healthDetail=e.message}
  render();
}

async function doFfmpeg(){
  try{const[f,p]=await invoke('detect_ffmpeg');S.ffmpegInfo=f;S.ffprobeInfo=p}catch(e){S.ffmpegInfo={available:false,error:String(e)};S.ffprobeInfo=S.ffmpegInfo}
  render();
}

async function doSelectFolder(){
  if(!window.__TAURI__){alert('需要 Tauri App');return}
  try{
    const s=await window.__TAURI__.dialog.open({directory:true,multiple:false,title:'选择素材文件夹'});
    if(s){S.folderPath=s;sc({lastFolder:s});S.batchResult=null;
      try{S.scanResult=await invoke('scan_folder',{folder:s})}catch{S.scanResult=null}
      render();}
  }catch(e){console.error(e)}
}

async function doBatch(){
  if(!S.folderPath||S.batchRunning)return;
  S.batchRunning=true;S.batchResult=null;render();
  try{
    S.batchResult=await invoke('batch_upload',{
      serverUrl:S.serverUrl, folder:S.folderPath,
      videoTheme:S.videoTheme||'', newsEvent:S.scriptText||''
    });
    if(S.batchResult?.overall_success&&S.batchResult?.task_url){openUrl(S.batchResult.task_url)}
  }catch(e){S.batchResult={files:[],total:0,ok_count:0,skipped:0,failed:1,uploaded:0,notify_ok:false,notify_status:String(e),overall_success:false,task_id:'',task_url:''}}
  S.batchRunning=false;render();
}

function openUrl(u){if(window.__TAURI__)window.__TAURI__.shell.open(u);else window.open(u,'_blank')}

function bind(){
  const u=document.getElementById('i-url');if(u)u.onchange=e=>{S.serverUrl=e.target.value.trim();sc({serverUrl:S.serverUrl})};
  const t=document.getElementById('i-theme');if(t)t.onchange=e=>{S.videoTheme=e.target.value;sc({videoTheme:S.videoTheme})};
  const s=document.getElementById('i-script');if(s)s.oninput=e=>{S.scriptText=e.target.value;sc({scriptText:S.scriptText})};
}

function init(){const c=lc();S.serverUrl=c.serverUrl||'http://47.93.194.154:8088';S.scriptText=c.scriptText||'';S.videoTheme=c.videoTheme||'';S.folderPath=c.lastFolder||'';render()}
window.doHealth=doHealth;window.doFfmpeg=doFfmpeg;window.doSelectFolder=doSelectFolder;window.doBatch=doBatch;window.openUrl=openUrl;
init();
