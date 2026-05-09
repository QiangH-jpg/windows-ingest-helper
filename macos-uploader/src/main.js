/**
 * 元泉智影上传助手 — Phase 3E
 * 实机可用性修复：health 走 Rust / FFmpeg 路径增强 / 进度可见
 */
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { open as shellOpen } from '@tauri-apps/plugin-shell';

const CK = "openclaw_uploader_config";
function lc(){try{return JSON.parse(localStorage.getItem(CK))||{}}catch{return{}}}
function sc(p){const c={...lc(),...p};localStorage.setItem(CK,JSON.stringify(c));return c}

const S = {
  serverUrl:"", healthStatus:"wait", healthDetail:"",
  folderPath:"", scriptText:"", videoTheme:"",
  ffmpegInfo:null, ffprobeInfo:null,
  scanResult:null, batchResult:null, batchRunning:false,
  diagInfo:null, showDiag:false,
};

function render(){
  document.getElementById("app").innerHTML=`
    <h1>✂️ 元泉智影上传助手</h1>
    <p class="subtitle">macOS 素材转码 / 上传客户端</p>

    <div class="section">
      <div class="section-title">🔗 服务器连接</div>
      <div class="row"><input type="text" id="i-url" value="${e(S.serverUrl)}" placeholder="http://47.93.194.154:8088" /><button onclick="doHealth()">测试连接</button></div>
      <div style="margin-top:6px">${badge(S.healthStatus,S.healthDetail)}</div>
    </div>

    <div class="section">
      <div class="section-title">🎬 FFmpeg 检测</div>
      <button onclick="doFfmpeg()">检测 FFmpeg</button>
      ${ffmpegHtml()}
    </div>

    <div class="section">
      <div class="section-title">📝 视频信息</div>
      <input type="text" id="i-theme" value="${e(S.videoTheme)}" placeholder="视频主题（如：新闻发布会）" style="margin-bottom:8px;width:100%" />
      <textarea id="i-script" placeholder="新闻事件摘要…">${e(S.scriptText)}</textarea>
    </div>

    <div class="section">
      <div class="section-title">📁 素材目录</div>
      <p style="font-size:11px;color:var(--text-sec);margin:0 0 8px 0">点击下方按钮选择包含视频的<b>文件夹</b>（不是选择单个文件，文件显示灰色是正常的）</p>
      <div class="row" style="gap:12px">
        <button onclick="doSelectFolder()">选择素材文件夹</button>
        <button onclick="doBatch()" class="primary" ${!canBatch()?'disabled':''}>${S.batchRunning?'⏳ 处理中…':'开始转码上传'}</button>
      </div>
      ${S.folderPath?`<div class="folder-path">📂 ${e(S.folderPath)}</div>`:''}
      ${scanHtml()}
      ${!canBatch()&&S.folderPath?batchBlockReason():''}
    </div>

    ${batchRunningHtml()}
    ${batchResultHtml()}

    <div class="section" style="border-top:1px solid var(--border);padding-top:12px;margin-top:16px">
      <button onclick="toggleDiag()" style="font-size:11px;padding:4px 10px;background:var(--bg-sec)">${S.showDiag?'隐藏':'🔧 诊断信息'}</button>
      ${S.showDiag?diagHtml():''}
    </div>

    <div class="footer">元泉智影上传助手 v0.1.0-alpha · Phase 3E</div>
  `;
  bind();
}

function e(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function badge(s,d){if(s==='ok')return`<span class="status ok">✅ 已连接${d?' · '+e(d):''}</span>`;if(s==='fail')return`<span class="status fail">❌ ${e(d||'失败')}</span>`;if(s==='checking')return`<span class="status wait">⏳ 检测中…</span>`;return'<span class="status wait">⏳ 未测试</span>'}
function fmtSize(b){if(b>1048576)return(b/1048576).toFixed(1)+'MB';if(b>1024)return(b/1024).toFixed(0)+'KB';return b+'B'}

function canBatch(){return S.folderPath && S.healthStatus==='ok' && !S.batchRunning && S.ffmpegInfo?.available}
function batchBlockReason(){
  const reasons=[];
  if(S.healthStatus!=='ok')reasons.push('服务器未连接');
  if(!S.ffmpegInfo?.available)reasons.push('FFmpeg 未检测到');
  if(!reasons.length)return'';
  return`<div style="margin-top:8px;font-size:12px;color:var(--danger)">⚠️ 无法开始：${reasons.join('、')}</div>`;
}

function ffmpegHtml(){
  if(!S.ffmpegInfo)return'<div style="margin-top:6px;font-size:12px;color:var(--text-sec)">点击上方按钮检测</div>';
  const f=S.ffmpegInfo,p=S.ffprobeInfo;
  let h='<div style="margin-top:8px;font-size:12px">';
  h+=`<div>${f.available?'✅':'❌'} <b>ffmpeg</b></div>`;
  h+=`<div style="color:var(--text-sec);margin-left:20px;word-break:break-all">路径: ${e(f.path)}</div>`;
  if(f.available)h+=`<div style="color:var(--text-sec);margin-left:20px">${e(f.version)}</div>`;
  if(!f.available)h+=`<div style="color:var(--danger);margin-left:20px">${e(f.error)}</div>`;
  h+=`<div style="margin-top:6px">${p.available?'✅':'❌'} <b>ffprobe</b></div>`;
  h+=`<div style="color:var(--text-sec);margin-left:20px;word-break:break-all">路径: ${e(p.path)}</div>`;
  if(p.available)h+=`<div style="color:var(--text-sec);margin-left:20px">${e(p.version)}</div>`;
  if(!p.available)h+=`<div style="color:var(--danger);margin-left:20px">${e(p.error)}</div>`;
  if(!f.available||!p.available){
    h+=`<div style="margin-top:8px;padding:8px;background:rgba(245,158,11,0.1);border-radius:4px;font-size:11px;color:var(--text-sec)">`;
    h+=`💡 请确认已安装 FFmpeg：<code>brew install ffmpeg</code><br>`;
    h+=`或终端验证：<code>/opt/homebrew/bin/ffmpeg -version</code></div>`;
  }
  h+='</div>';
  return h;
}

function scanHtml(){
  const r=S.scanResult;if(!r)return'';
  let h=`<div style="margin-top:8px;font-size:12px;color:var(--text-sec)">发现 <b style="color:var(--text)">${r.total}</b> 个视频文件${r.skipped_hidden?' · '+r.skipped_hidden+' 隐藏跳过':''}${r.skipped_small?' · '+r.skipped_small+' 过小跳过':''}</div>`;
  if(r.files&&r.files.length>0){
    h+='<div style="margin-top:6px;font-size:11px;max-height:120px;overflow-y:auto">';
    for(const f of r.files)h+=`<div style="padding:2px 0;color:var(--text-sec)">🎬 ${e(f.filename)} <span style="opacity:0.6">(${fmtSize(f.size)})</span></div>`;
    h+='</div>';
  }
  return h;
}

function batchRunningHtml(){
  if(!S.batchRunning)return'';
  return`<div class="section"><div class="section-title">⏳ 正在处理</div>
    <div style="font-size:13px;color:var(--text-sec)">
      <div>正在串行转码 → 上传中，请勿关闭窗口…</div>
      <div style="margin-top:8px">📂 转码输出目录：<code>/tmp/openclaw_uploader_proxy/</code></div>
      <div style="margin-top:4px;font-size:11px;color:var(--text-sec)">每个文件依次：probe → 转码 720p → presign → PUT 上传</div>
    </div></div>`;
}

function batchResultHtml(){
  const r=S.batchResult;if(!r)return'';
  let h=`<div class="section"><div class="section-title">📊 处理结果</div>`;
  const ok=r.overall_success;
  h+=`<div style="background:${ok?'rgba(16,185,129,0.1)':'rgba(245,158,11,0.1)'};border:1px solid ${ok?'rgba(16,185,129,0.3)':'rgba(245,158,11,0.3)'};border-radius:6px;padding:12px;font-size:13px;margin-bottom:12px">`;
  h+=`<div style="font-weight:600;margin-bottom:4px">${ok?'✅ 全部成功':'⚠️ 处理完成'}</div>`;
  h+=`<div style="color:var(--text-sec)">总计 ${r.total} · 已上传 ${r.uploaded} · 跳过 ${r.skipped} · 失败 ${r.failed}</div>`;
  if(r.task_id)h+=`<div style="color:var(--text-sec);margin-top:4px">任务 ID：${e(r.task_id)}</div>`;
  if(r.notify_status)h+=`<div style="color:var(--text-sec)">通知状态：${e(r.notify_status)}</div>`;
  if(r.proxy_dir)h+=`<div style="color:var(--text-sec);margin-top:4px;font-size:11px">转码输出：${e(r.proxy_dir)}</div>`;
  if(r.task_url&&r.notify_ok)h+=`<div style="margin-top:8px"><button onclick="doOpenUrl('${e(r.task_url)}')" class="primary" style="font-size:12px;padding:5px 12px">🌐 打开 Web 工作台</button></div>`;
  h+=`</div>`;
  // 文件列表
  h+=`<div style="font-size:12px">`;
  for(const f of r.files){
    const icon=f.status==='已上传'?'✅':f.status.startsWith('跳过')?'⏭️':'❌';
    h+=`<div style="padding:6px 0;border-bottom:1px solid var(--border)">`;
    h+=`<div style="display:flex;gap:6px;align-items:center">${icon} <b>${e(f.filename)}</b> <span style="color:var(--text-sec);font-size:11px">${e(f.status)}</span></div>`;
    const details=[];
    if(f.transcode_ok)details.push(`转码 ${f.transcode_time.toFixed(1)}s`);
    if(f.proxy_size>0)details.push(`proxy ${fmtSize(f.proxy_size)}`);
    if(f.upload_ok)details.push('PUT 200');
    if(f.object_key)details.push(f.object_key.split('/').pop());
    if(details.length)h+=`<div style="color:var(--text-sec);font-size:11px;margin-left:24px">${details.join(' · ')}</div>`;
    if(f.proxy_path)h+=`<div style="color:var(--text-sec);font-size:10px;margin-left:24px;word-break:break-all">📄 ${e(f.proxy_path)}</div>`;
    if(f.error)h+=`<div style="color:var(--danger);font-size:11px;margin-left:24px">${e(f.error.substring(0,150))}</div>`;
    h+=`</div>`;
  }
  h+=`</div></div>`;
  return h;
}

function diagHtml(){
  const d=S.diagInfo;
  if(!d)return'<div style="font-size:11px;color:var(--text-sec);margin-top:8px">加载中…</div>';
  return`<div style="margin-top:8px;font-size:11px;color:var(--text-sec);word-break:break-all">
    <div><b>版本</b>: ${e(d.app_version)}</div>
    <div><b>可执行文件</b>: ${e(d.exe_path)}</div>
    <div><b>ffmpeg</b>: ${e(d.ffmpeg_path)}</div>
    <div><b>ffprobe</b>: ${e(d.ffprobe_path)}</div>
    <div><b>转码输出</b>: ${e(d.proxy_dir)}</div>
    <div><b>服务器</b>: ${e(S.serverUrl)}</div>
    <div><b>素材目录</b>: ${e(S.folderPath||'未选择')}</div>
  </div>`;
}

// ============================================================
async function doHealth(){
  const url=S.serverUrl||document.getElementById('i-url')?.value?.trim();
  if(!url){S.healthStatus='fail';S.healthDetail='请输入服务器地址';render();return}
  S.serverUrl=url;sc({serverUrl:url});S.healthStatus='checking';S.healthDetail='';render();
  try{
    const r=await invoke('check_health',{serverUrl:url});
    if(r.ok){S.healthStatus='ok';S.healthDetail=r.version||'连接成功'}
    else{S.healthStatus='fail';S.healthDetail=r.error||`HTTP ${r.status}`}
  }catch(err){S.healthStatus='fail';S.healthDetail=String(err)}
  render();
}

async function doFfmpeg(){
  try{const[f,p]=await invoke('detect_ffmpeg');S.ffmpegInfo=f;S.ffprobeInfo=p}catch(err){S.ffmpegInfo={available:false,error:String(err),path:'',version:'',exists:false,executable:false};S.ffprobeInfo=S.ffmpegInfo}
  render();
}

async function doSelectFolder(){
  try{
    const selected=await openDialog({directory:true,multiple:false,title:'请选择包含视频的文件夹（不是选择单个文件）'});
    if(selected){S.folderPath=selected;sc({lastFolder:selected});S.batchResult=null;
      try{S.scanResult=await invoke('scan_folder',{folder:selected})}catch(err){S.scanResult=null}
      render();}
  }catch(err){alert('目录选择失败：'+String(err))}
}

async function doBatch(){
  if(!canBatch()||S.batchRunning)return;
  S.batchRunning=true;S.batchResult=null;render();
  try{
    S.batchResult=await invoke('batch_upload',{
      serverUrl:S.serverUrl,folder:S.folderPath,
      videoTheme:S.videoTheme||'',newsEvent:S.scriptText||''
    });
    if(S.batchResult?.overall_success&&S.batchResult?.task_url)doOpenUrl(S.batchResult.task_url);
  }catch(err){S.batchResult={files:[],total:0,ok_count:0,skipped:0,failed:1,uploaded:0,notify_ok:false,notify_status:String(err),overall_success:false,task_id:'',task_url:'',proxy_dir:'/tmp/openclaw_uploader_proxy'}}
  S.batchRunning=false;render();
}

async function doOpenUrl(u){try{await shellOpen(u)}catch{window.open(u,'_blank')}}
async function toggleDiag(){S.showDiag=!S.showDiag;if(S.showDiag&&!S.diagInfo){try{S.diagInfo=await invoke('get_diag_info')}catch{}}render()}

function bind(){
  const u=document.getElementById('i-url');if(u)u.onchange=ev=>{S.serverUrl=ev.target.value.trim();sc({serverUrl:S.serverUrl})};
  const t=document.getElementById('i-theme');if(t)t.onchange=ev=>{S.videoTheme=ev.target.value;sc({videoTheme:S.videoTheme})};
  const s=document.getElementById('i-script');if(s)s.oninput=ev=>{S.scriptText=ev.target.value;sc({scriptText:S.scriptText})};
}

function init(){const c=lc();S.serverUrl=c.serverUrl||'http://47.93.194.154:8088';S.scriptText=c.scriptText||'';S.videoTheme=c.videoTheme||'';S.folderPath=c.lastFolder||'';render()}
window.doHealth=doHealth;window.doFfmpeg=doFfmpeg;window.doSelectFolder=doSelectFolder;window.doBatch=doBatch;window.doOpenUrl=doOpenUrl;window.toggleDiag=toggleDiag;
init();
