/**
 * 元泉智影上传助手 — Phase 3G
 * Windows V15.3.2 对齐 + 逐文件实时进度
 */
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { open as shellOpen } from '@tauri-apps/plugin-shell';

const CK="openclaw_uploader_config";
function lc(){try{return JSON.parse(localStorage.getItem(CK))||{}}catch{return{}}}
function sc(p){const c={...lc(),...p};localStorage.setItem(CK,JSON.stringify(c));return c}
function e(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmtSize(b){if(b>1048576)return(b/1048576).toFixed(1)+'MB';if(b>1024)return(b/1024).toFixed(0)+'KB';return b+'B'}
function fmtDur(s){if(!s)return'-';return s.toFixed(1)+'s'}

const PROXY_DIR="/tmp/openclaw_uploader_proxy";

const S={
  serverUrl:"",healthOk:false,healthMsg:"未测试",
  videoTheme:"",newsEvent:"",
  folderPath:"",
  ffmpegOk:false,ffmpegMsg:"",ffprobeOk:false,ffprobeMsg:"",
  ffmpegPath:"",ffprobePath:"",
  files:[],           // [{path,filename,size,duration,width,height,status,probeInfo,proxyPath,proxySize,tcTime,objectKey,error}]
  processing:false,
  phase:"idle",       // idle|scanning|probing|transcoding|uploading|notifying|done|failed
  currentIdx:-1,
  taskId:"",taskUrl:"",
  totalUploaded:0,totalSkipped:0,totalFailed:0,
  logs:[],
  showDiag:false,diagInfo:null,
};

function log(msg){S.logs.push(`[${new Date().toLocaleTimeString()}] ${msg}`);renderLog()}
function renderLog(){const el=document.getElementById('log-area');if(el){el.innerHTML=S.logs.map(l=>`<div>${e(l)}</div>`).join('');el.scrollTop=el.scrollHeight}}

function render(){
  document.getElementById("app").innerHTML=`
  <div class="toolbar">✂️ 元泉智影上传助手 <span class="ver">v0.1.0</span></div>

  <div class="panel">
    <div class="panel-title">1. 服务器连接</div>
    <div class="row"><input type="text" id="i-url" value="${e(S.serverUrl)}" placeholder="http://47.93.194.154:8088" class="flex1"/><button onclick="doHealth()">测试连接</button></div>
    <div class="status-line">${S.healthOk?'✅':'⏳'} ${e(S.healthMsg)}</div>
  </div>

  <div class="panel">
    <div class="panel-title">2. 任务信息</div>
    <div class="field-row"><label>视频主题</label><input type="text" id="i-theme" value="${e(S.videoTheme)}" placeholder="如：新闻发布会"/></div>
    <div class="field-row"><label>新闻事件</label><textarea id="i-event" rows="2" placeholder="新闻事件摘要…">${e(S.newsEvent)}</textarea></div>
  </div>

  <div class="panel">
    <div class="panel-title">3. FFmpeg</div>
    <button onclick="doFfmpeg()">检测</button>
    <div class="status-line">${S.ffmpegOk?'✅ ffmpeg':'❌ ffmpeg'} <span class="dim">${e(S.ffmpegPath)}</span></div>
    <div class="status-line">${S.ffprobeOk?'✅ ffprobe':'❌ ffprobe'} <span class="dim">${e(S.ffprobePath)}</span></div>
    ${S.ffmpegMsg?`<div class="dim">${e(S.ffmpegMsg)}</div>`:''}
  </div>

  <div class="panel">
    <div class="panel-title">4. 素材目录</div>
    <div class="row">
      <button onclick="doSelectFolder()">选择素材文件夹</button>
      <button onclick="doStart()" class="primary" ${canStart()?'':'disabled'}>${S.processing?'⏳ 处理中…':'开始转码上传'}</button>
    </div>
    ${S.folderPath?`<div class="dim" style="margin-top:4px">📂 ${e(S.folderPath)}</div>`:''}
    ${!canStart()&&S.folderPath?blockReason():''}
  </div>

  <div class="panel">
    <div class="panel-title">5. 视频列表 ${S.files.length?`(${S.files.length} 个)`:''} ${progressSummary()}</div>
    <div class="file-table">${fileTableHtml()}</div>
  </div>

  ${taskResultHtml()}

  <div class="panel">
    <div class="panel-title">6. 处理日志 <button onclick="toggleDiag()" class="sm">${S.showDiag?'隐藏诊断':'🔧 诊断'}</button></div>
    ${S.showDiag?diagHtml():''}
    <div id="log-area" class="log-area">${S.logs.map(l=>`<div>${e(l)}</div>`).join('')}</div>
  </div>
  `;
  bind();
}

function canStart(){return S.folderPath&&S.healthOk&&!S.processing&&S.ffmpegOk&&S.files.length>0}
function blockReason(){
  const r=[];
  if(!S.healthOk)r.push('服务器未连接');
  if(!S.ffmpegOk)r.push('FFmpeg 未检测到');
  if(!S.files.length)r.push('无视频文件');
  return r.length?`<div class="warn">⚠️ ${r.join('、')}</div>`:'';
}

function progressSummary(){
  if(!S.processing&&S.phase==='idle')return'';
  if(S.phase==='done')return`<span class="ok">✅ 完成 上传${S.totalUploaded} 跳过${S.totalSkipped} 失败${S.totalFailed}</span>`;
  if(S.phase==='failed')return`<span class="err">❌ 失败</span>`;
  const idx=S.currentIdx+1,tot=S.files.length;
  const phases={scanning:'扫描中',probing:'分析中',transcoding:'转码中',uploading:'上传中',notifying:'通知中'};
  return`<span class="dim">${phases[S.phase]||S.phase} ${idx}/${tot}</span>`;
}

function fileTableHtml(){
  if(!S.files.length)return'<div class="dim">选择文件夹后显示视频列表</div>';
  let h='<div class="fthead"><span class="c-idx">#</span><span class="c-name">文件名</span><span class="c-dur">时长</span><span class="c-res">分辨率</span><span class="c-size">大小</span><span class="c-status">状态</span></div>';
  S.files.forEach((f,i)=>{
    const icon=f.status==='uploaded'?'✅':f.status==='skipped'?'⏭️':f.status==='failed'||f.status==='tc_failed'?'❌':f.status==='transcoding'||f.status==='uploading'?'⏳':f.status==='probing'?'🔍':'⬜';
    const cls=i===S.currentIdx&&S.processing?'ftrow active':'ftrow';
    h+=`<div class="${cls}"><span class="c-idx">${i+1}</span><span class="c-name" title="${e(f.filename)}">${icon} ${e(f.filename)}</span><span class="c-dur">${fmtDur(f.duration)}</span><span class="c-res">${f.width?f.width+'×'+f.height:'-'}</span><span class="c-size">${fmtSize(f.size)}</span><span class="c-status">${statusText(f)}</span></div>`;
  });
  return h;
}

function statusText(f){
  const m={pending:'等待',probing:'分析中…',probed:'已分析',skipped:'跳过',transcoding:'转码中…',transcoded:'已转码',tc_failed:'转码失败',uploading:'上传中…',uploaded:'已上传',failed:'失败'};
  let t=m[f.status]||f.status;
  if(f.tcTime>0)t+=` ${f.tcTime.toFixed(1)}s`;
  if(f.proxySize>0)t+=` ${fmtSize(f.proxySize)}`;
  if(f.objectKey)t+=` ✅`;
  if(f.error)t=`<span class="err">${e(f.error.substring(0,60))}</span>`;
  return t;
}

function taskResultHtml(){
  if(S.phase!=='done'&&S.phase!=='failed')return'';
  let h=`<div class="panel result-panel"><div class="panel-title">📊 处理结果</div>`;
  h+=`<div>任务 ID：${e(S.taskId)}</div>`;
  h+=`<div>上传 ${S.totalUploaded} · 跳过 ${S.totalSkipped} · 失败 ${S.totalFailed}</div>`;
  h+=`<div>转码输出：${e(PROXY_DIR)}</div>`;
  if(S.taskUrl)h+=`<div style="margin-top:8px"><button onclick="doOpenUrl('${e(S.taskUrl)}')" class="primary">🌐 打开 Web 工作台</button></div>`;
  h+=`</div>`;
  return h;
}

function diagHtml(){
  if(!S.diagInfo)return'<div class="dim">加载中…</div>';
  const d=S.diagInfo;
  return`<div class="diag"><div>版本: ${e(d.app_version)}</div><div>exe: ${e(d.exe_path)}</div><div>ffmpeg: ${e(d.ffmpeg_path)}</div><div>ffprobe: ${e(d.ffprobe_path)}</div><div>proxy: ${e(d.proxy_dir)}</div><div>server: ${e(S.serverUrl)}</div></div>`;
}

// ============================================================
// Actions
// ============================================================
async function doHealth(){
  const url=S.serverUrl||document.getElementById('i-url')?.value?.trim();
  if(!url){S.healthOk=false;S.healthMsg='请输入地址';render();return}
  S.serverUrl=url;sc({serverUrl:url});S.healthMsg='连接中…';render();
  try{
    const r=await invoke('check_health',{serverUrl:url});
    S.healthOk=r.ok;S.healthMsg=r.ok?`已连接 · ${r.version||'ok'}`:r.error||`HTTP ${r.status}`;
    log(S.healthOk?`✅ 服务器连接成功 ${r.version}`:`❌ 服务器连接失败: ${r.error}`);
  }catch(err){S.healthOk=false;S.healthMsg=String(err);log(`❌ ${err}`)}
  render();
}

async function doFfmpeg(){
  try{
    const[f,p]=await invoke('detect_ffmpeg');
    S.ffmpegOk=f.available;S.ffmpegPath=f.path;S.ffmpegMsg=f.available?f.version:f.error;
    S.ffprobeOk=p.available;S.ffprobePath=p.path;
    log(f.available?`✅ ffmpeg: ${f.version}`:`❌ ffmpeg: ${f.error}`);
    log(p.available?`✅ ffprobe: ${p.version}`:`❌ ffprobe: ${p.error}`);
  }catch(err){S.ffmpegOk=false;S.ffmpegMsg=String(err);log(`❌ ${err}`)}
  render();
}

async function doSelectFolder(){
  try{
    const sel=await openDialog({directory:true,multiple:false,title:'请选择包含视频的文件夹'});
    if(!sel)return;
    S.folderPath=sel;sc({lastFolder:sel});S.files=[];S.phase='scanning';S.logs=[];render();
    log(`📂 选择目录: ${sel}`);
    const scan=await invoke('scan_folder',{folder:sel});
    log(`发现 ${scan.total} 个视频文件${scan.skipped_hidden?' ('+scan.skipped_hidden+' 隐藏跳过)':''}${scan.skipped_small?' ('+scan.skipped_small+' 过小跳过)':''}`);
    S.files=scan.files.map(f=>({...f,duration:0,width:0,height:0,status:'pending',probeInfo:null,proxyPath:'',proxySize:0,tcTime:0,objectKey:'',error:''}));
    S.phase='idle';
  }catch(err){log(`❌ 目录选择失败: ${err}`);S.phase='idle'}
  render();
}

async function doStart(){
  if(!canStart()||S.processing)return;
  S.processing=true;S.totalUploaded=0;S.totalSkipped=0;S.totalFailed=0;S.taskId='';S.taskUrl='';
  // reset statuses
  S.files.forEach(f=>{f.status='pending';f.proxyPath='';f.proxySize=0;f.tcTime=0;f.objectKey='';f.error=''});
  render();

  // 1. Probe all
  S.phase='probing';render();
  const okFiles=[];
  for(let i=0;i<S.files.length;i++){
    S.currentIdx=i;S.files[i].status='probing';render();
    log(`🔍 分析 ${i+1}/${S.files.length}: ${S.files[i].filename}`);
    try{
      const info=await invoke('probe_video',{path:S.files[i].path});
      S.files[i].duration=info.duration;S.files[i].width=info.width;S.files[i].height=info.height;
      if(info.status==='ok'){S.files[i].status='probed';okFiles.push(i);log(`  ✅ ${info.width}×${info.height} ${fmtDur(info.duration)}`)}
      else{S.files[i].status='skipped';S.files[i].error=info.status_reason;S.totalSkipped++;log(`  ⏭️ 跳过: ${info.status_reason}`)}
    }catch(err){S.files[i].status='skipped';S.files[i].error=String(err);S.totalSkipped++;log(`  ❌ ${err}`)}
    render();
  }

  if(!okFiles.length){S.phase='done';S.processing=false;log('⚠️ 没有可处理的视频');render();return}

  // 2. task/init
  log(`📡 创建任务 (${okFiles.length} 个文件)…`);
  const filenames=okFiles.map(i=>S.files[i].filename);
  try{
    const init=await invoke('task_init',{serverUrl:S.serverUrl,fileCount:okFiles.length,filenames,videoTheme:S.videoTheme,newsEvent:S.newsEvent});
    if(!init.ok){S.phase='failed';S.processing=false;log(`❌ 创建任务失败: ${init.error}`);render();return}
    S.taskId=init.task_id;S.taskUrl=init.task_url;
    log(`✅ 任务创建成功: ${init.task_id}`);
  }catch(err){S.phase='failed';S.processing=false;log(`❌ ${err}`);render();return}

  // 3. Transcode + Upload each
  const uploadedKeys=[];
  for(let j=0;j<okFiles.length;j++){
    const i=okFiles[j];
    const f=S.files[i];
    const stem=f.filename.replace(/\.[^.]+$/,'').replace(/[^a-zA-Z0-9_-]/g,'_');
    const proxyPath=`${PROXY_DIR}/proxy_${String(j).padStart(4,'0')}_${stem}.mp4`;

    // Transcode
    S.phase='transcoding';S.currentIdx=i;f.status='transcoding';render();
    log(`🎬 转码 ${j+1}/${okFiles.length}: ${f.filename}`);
    try{
      const tc=await invoke('transcode_video',{inputPath:f.path,outputPath:proxyPath});
      if(!tc.ok){f.status='tc_failed';f.error=tc.error;S.totalFailed++;log(`  ❌ 转码失败: ${tc.error}`);render();continue}
      f.proxyPath=tc.proxy_path;f.proxySize=tc.proxy_size;f.tcTime=tc.time_secs;f.status='transcoded';
      log(`  ✅ 转码完成 ${tc.time_secs.toFixed(1)}s → ${fmtSize(tc.proxy_size)}`);
    }catch(err){f.status='tc_failed';f.error=String(err);S.totalFailed++;log(`  ❌ ${err}`);render();continue}
    render();

    // Upload
    S.phase='uploading';f.status='uploading';render();
    log(`📤 上传 ${j+1}/${okFiles.length}: ${f.filename}`);
    const proxyFn=proxyPath.split('/').pop();
    try{
      const up=await invoke('upload_file',{serverUrl:S.serverUrl,taskId:S.taskId,proxyPath:f.proxyPath,filename:proxyFn});
      if(!up.ok){f.status='failed';f.error=up.error;S.totalFailed++;log(`  ❌ 上传失败: ${up.error}`);render();continue}
      f.status='uploaded';f.objectKey=up.object_key;S.totalUploaded++;uploadedKeys.push(up.object_key);
      log(`  ✅ PUT 200 → ${up.object_key.split('/').pop()}`);
    }catch(err){f.status='failed';f.error=String(err);S.totalFailed++;log(`  ❌ ${err}`);render();continue}
    render();
  }

  // 4. Notify
  if(uploadedKeys.length>0){
    S.phase='notifying';render();
    log(`📡 通知服务器 (${uploadedKeys.length} 个文件)…`);
    try{
      const n=await invoke('task_notify',{serverUrl:S.serverUrl,taskId:S.taskId,tosKeys:uploadedKeys,fileCount:uploadedKeys.length});
      if(n.ok)log(`✅ notify: ${n.status}`);else log(`⚠️ notify: ${n.error}`);
    }catch(err){log(`⚠️ notify 失败: ${err}`)}
  }

  S.phase='done';S.processing=false;S.currentIdx=-1;
  log(`\n🎉 处理完成！上传 ${S.totalUploaded} · 跳过 ${S.totalSkipped} · 失败 ${S.totalFailed}`);
  if(S.taskUrl)log(`🌐 Web 工作台: ${S.taskUrl}`);
  render();
}

async function doOpenUrl(u){try{await shellOpen(u)}catch{window.open(u,'_blank')}}
async function toggleDiag(){S.showDiag=!S.showDiag;if(S.showDiag&&!S.diagInfo){try{S.diagInfo=await invoke('get_diag_info')}catch{}}render()}

function bind(){
  const u=document.getElementById('i-url');if(u)u.onchange=ev=>{S.serverUrl=ev.target.value.trim();sc({serverUrl:S.serverUrl})};
  const t=document.getElementById('i-theme');if(t)t.onchange=ev=>{S.videoTheme=ev.target.value;sc({videoTheme:S.videoTheme})};
  const s=document.getElementById('i-event');if(s)s.oninput=ev=>{S.newsEvent=ev.target.value;sc({newsEvent:S.newsEvent})};
}

function init(){const c=lc();S.serverUrl=c.serverUrl||'http://47.93.194.154:8088';S.newsEvent=c.newsEvent||'';S.videoTheme=c.videoTheme||'';S.folderPath=c.lastFolder||'';render()}
window.doHealth=doHealth;window.doFfmpeg=doFfmpeg;window.doSelectFolder=doSelectFolder;window.doStart=doStart;window.doOpenUrl=doOpenUrl;window.toggleDiag=toggleDiag;
init();
