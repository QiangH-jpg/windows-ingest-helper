/**
 * 元泉智影上传助手 — Phase 3G
 * Windows V15.3.2 对齐（按真实截图）+ 逐文件实时进度
 */
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import { open as shellOpen } from '@tauri-apps/plugin-shell';

const CK="openclaw_uploader_config";
const CLIENT_VERSION="3.2.1";
function lc(){try{return JSON.parse(localStorage.getItem(CK))||{}}catch{return{}}}
function sc(p){const c={...lc(),...p};localStorage.setItem(CK,JSON.stringify(c));return c}
function e(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmtSize(b){if(b>1048576)return(b/1048576).toFixed(1)+'MB';if(b>1024)return(b/1024).toFixed(0)+'KB';return b+'B'}
function fmtDur(s){if(!s)return'-';return s.toFixed(1)+'秒'}

const PROXY_DIR="/tmp/openclaw_uploader_proxy";

const S={
  serverUrl:"",healthOk:false,healthMsg:"未测试",
  serverVersion:"", // v13.0.1: 服务端版本号
  videoTheme:"",newsEvent:"",
  inputDir:"",outputDir:PROXY_DIR,
  folderPath:"",
  ffmpegOk:false,ffmpegMsg:"",ffprobeOk:false,ffprobeMsg:"",
  ffmpegPath:"",ffprobePath:"",
  files:[],
  processing:false,
  phase:"idle",
  currentIdx:-1,
  taskId:"",taskUrl:"",
  totalUploaded:0,totalSkipped:0,totalFailed:0,
  logs:[],
  showDiag:false,diagInfo:null,
};

function log(msg){S.logs.push(`[${new Date().toLocaleTimeString('zh-CN',{hour12:false})}] ${msg}`);renderLog()}
function renderLog(){const el=document.getElementById('log-area');if(el){el.innerHTML=S.logs.map(l=>`<div>${e(l)}</div>`).join('');setTimeout(()=>{el.scrollTop=el.scrollHeight},0)}}

function render(){
  document.getElementById("app").innerHTML=`
  <div class="toolbar">✂️ 元泉智影上传助手 <span class="ver">v${CLIENT_VERSION}</span>${S.serverVersion?` <span class="ver" style="opacity:0.6">Server ${e(S.serverVersion)}</span>`:''} <span class="ver" style="opacity:0.5;font-size:11px">${S.healthOk?'✅ Connected':'❌'}</span></div>

  <div class="section">
    <div class="section-title">1. 选择目录</div>
    <div class="field-row">
      <label>素材目录:</label>
      <input type="text" id="i-input" value="${e(S.inputDir)}" readonly class="flex1"/>
      <button id="btn-browse-input">浏览...</button>
    </div>
    <div class="field-row">
      <label>输出目录:</label>
      <input type="text" id="i-output" value="${e(S.outputDir)}" readonly class="flex1"/>
      <button id="btn-browse-output">浏览...</button>
    </div>
  </div>

  <div class="section">
    <div class="section-title">2. 任务语境</div>
    <div class="field-row">
      <label>视频主题:</label>
      <input type="text" id="i-theme" value="${e(S.videoTheme)}" class="flex1" placeholder="如：人社服务大篷车"/>
    </div>
    <div class="field-row">
      <label>新闻事件:</label>
      <textarea id="i-event" rows="2" class="flex1" placeholder="新闻事件摘要…">${e(S.newsEvent)}</textarea>
    </div>
  </div>

  <div class="section">
    <div class="section-title">3. 视频列表 ${S.files.length?`(${S.files.length} 个)`:''} ${progressSummary()}</div>
    <div class="video-list">
      <table class="file-table">
        <thead><tr><th class="col-name">文件名</th><th class="col-dur">时长</th><th class="col-res">分辨率</th><th class="col-size">大小</th><th class="col-status">状态</th></tr></thead>
        <tbody>${fileTableHtml()}</tbody>
      </table>
    </div>
    <div class="button-bar">
      <button id="btn-scan">扫描素材</button>
      <button id="btn-transcode" ${canTranscode()?'':'disabled'}>转码 Proxy</button>
      <button id="btn-upload" ${canUpload()?'':'disabled'}>上传 TOS</button>
      <div class="progress-wrap"><div class="progress-bar" style="width:${progressPct()}%"></div></div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">4. 处理日志 <button onclick="toggleDiag()" class="sm">${S.showDiag?'隐藏':'🔧 诊断'}</button></div>
    ${S.showDiag?diagHtml():''}
    <div id="log-area" class="log-area">${S.logs.map(l=>`<div>${e(l)}</div>`).join('')}</div>
  </div>

  <div class="status-bar">${statusBarText()}</div>
  `;
  bind();
  setTimeout(()=>{const el=document.getElementById('log-area');if(el)el.scrollTop=el.scrollHeight},10);
}

function canTranscode(){return S.inputDir&&!S.processing&&S.files.length>0}
function canUpload(){return !S.processing&&S.files.some(f=>f.status==='transcoded')}
function progressPct(){
  if(!S.files.length)return 0;
  if(S.phase==='done')return 100;
  if(S.phase==='failed')return 0;
  const done=S.files.filter(f=>f.status==='uploaded'||f.status==='skipped'||f.status==='failed').length;
  return Math.round(done/S.files.length*100);
}
function progressSummary(){
  if(!S.processing&&S.phase==='idle')return'';
  if(S.phase==='done')return`<span class="ok">✅ 完成 上传${S.totalUploaded} 跳过${S.totalSkipped} 失败${S.totalFailed}</span>`;
  if(S.phase==='failed')return`<span class="err">❌ 失败</span>`;
  const idx=S.currentIdx+1,tot=S.files.length;
  const phases={scanning:'扫描中',probing:'分析中',transcoding:'转码中',uploading:'上传中',notifying:'通知中'};
  return`<span class="dim">${phases[S.phase]||S.phase} ${idx}/${tot}</span>`;
}
function statusBarText(){
  if(S.phase==='done')return`转码完成：${S.totalUploaded} 成功，${S.totalFailed} 失败`;
  if(S.phase==='failed')return`处理失败`;
  if(S.processing)return`处理中… ${S.currentIdx+1}/${S.files.length}`;
  return`就绪`;
}

function fileTableHtml(){
  if(!S.files.length)return`<tr><td colspan="5" class="dim">扫描素材后显示视频列表</td></tr>`;
  return S.files.map((f,i)=>{
    const icon=f.status==='uploaded'?'✅':f.status==='skipped'?'⏭️':f.status==='failed'||f.status==='tc_failed'?'❌':f.status==='transcoding'||f.status==='uploading'?'⏳':f.status==='probing'?'🔍':f.status==='transcoded'?'✓':'⬜';
    const cls=i===S.currentIdx&&S.processing?'active':'';
    return`<tr class="${cls}"><td class="col-name" title="${e(f.filename)}">${icon} ${e(f.filename)}</td><td class="col-dur">${fmtDur(f.duration)}</td><td class="col-res">${f.width?f.width+'×'+f.height:'-'}</td><td class="col-size">${fmtSize(f.size)}</td><td class="col-status">${statusText(f)}</td></tr>`;
  }).join('');
}

function statusText(f){
  const m={pending:'待处理',probing:'分析中…',probed:'已分析',skipped:'跳过',transcoding:'转码中…',transcoded:'转码完成',tc_failed:'转码失败',uploading:'上传中…',uploaded:'上传完成',failed:'失败'};
  let t=m[f.status]||f.status;
  if(f.tcTime>0)t+=` ${f.tcTime.toFixed(1)}s`;
  if(f.proxySize>0)t+=` ${fmtSize(f.proxySize)}`;
  if(f.objectKey)t+=` ✅`;
  if(f.error)t=`<span class="err">${e(f.error.substring(0,50))}</span>`;
  return t;
}

function taskResultHtml(){
  if(S.phase!=='done'&&S.phase!=='failed')return'';
  return`<div class="section result-section"><div class="section-title">📊 处理结果</div>
    <div>task_id: ${e(S.taskId)}</div>
    <div>上传 ${S.totalUploaded} · 跳过 ${S.totalSkipped} · 失败 ${S.totalFailed}</div>
    <div>proxy 输出：${e(PROXY_DIR)}</div>
    ${S.taskUrl?`<div style="margin-top:8px"><button onclick="doOpenUrl('${e(S.taskUrl)}')" class="primary">🌐 打开 Web 工作台</button></div>`:''}
  </div>`;
}

function diagHtml(){
  if(!S.diagInfo)return'<div class="dim">加载中…</div>';
  const d=S.diagInfo;
  return`<div class="diag"><div>版本: ${e(d.app_version)}</div><div>exe: ${e(d.exe_path)}</div><div>ffmpeg: ${e(d.ffmpeg_path)}</div><div>ffprobe: ${e(d.ffprobe_path)}</div><div>proxy: ${e(d.proxy_dir)}</div><div>server: ${e(S.serverUrl)}</div></div>`;
}

// ============================================================
// Actions
// ============================================================
async function doBrowseInput(){
  log('[diag] click browse material');
  try{const sel=await openDialog({directory:true,multiple:false,title:'请选择素材文件夹（包含视频文件的目录）'});if(sel){log(`[diag] selected input: ${sel}`);S.inputDir=sel;sc({inputDir:sel});render()}else{log('[diag] cancelled input')}}catch(e){log(`[diag] doBrowseInput error: ${e.name} ${e.message}`)}
}
async function doBrowseOutput(){
  log('[diag] click browse output');
  try{const sel=await openDialog({directory:true,multiple:false,title:'请选择转码输出目录'});if(sel){log(`[diag] selected output: ${sel}`);S.outputDir=sel;sc({outputDir:sel});render()}else{log('[diag] cancelled output')}}catch(e){log(`[diag] doBrowseOutput error: ${e.name} ${e.message}`)}
}

async function doHealth(){
  const url=S.serverUrl||'http://47.93.194.154';
  S.serverUrl=url;sc({serverUrl:url});S.healthMsg='连接中…';render();
  try{const r=await invoke('check_health',{serverUrl:url});S.healthOk=r.ok;S.healthMsg=r.ok?`已连接 · ${r.version||'ok'}`:r.error||`HTTP ${r.status}`;log(S.healthOk?`✅ 服务器连接成功 ${r.version}`:`❌ 服务器连接失败: ${r.error}`)}catch(err){S.healthOk=false;S.healthMsg=String(err);log(`❌ ${err}`)}
  render();
}

async function doFfmpeg(){
  try{const[f,p]=await invoke('detect_ffmpeg');S.ffmpegOk=f.available;S.ffmpegPath=f.path;S.ffmpegMsg=f.available?f.version:f.error;S.ffprobeOk=p.available;S.ffprobePath=p.path;log(f.available?`✅ ffmpeg: ${f.version}`:`❌ ffmpeg: ${f.error}`);log(p.available?`✅ ffprobe: ${p.version}`:`❌ ffprobe: ${p.error}`)}catch(err){S.ffmpegOk=false;S.ffmpegMsg=String(err);log(`❌ ${err}`)}
  render();
}

async function doScan(){
  log('[diag] click scan materials');
  log(`[diag] material_dir=${S.inputDir||'(empty)'}`);
  log(`[diag] output_dir=${S.outputDir||'(empty)'}`);
  if(!S.inputDir){
  S.processing=true;S.phase='scanning';S.files=[];S.logs=[];render();
  log(`📂 扫描目录: ${S.inputDir}`);
  log('[diag] invoking scan command...');
  try{
    const scan=await invoke('scan_folder',{folder:S.inputDir});
    log(`[diag] scan result=${scan.total}`);
    log(`发现 ${scan.total} 个视频文件${scan.skipped_hidden?' ('+scan.skipped_hidden+' 隐藏跳过)':''}${scan.skipped_small?' ('+scan.skipped_small+' 过小跳过)':''}`);
    S.files=scan.files.map(f=>({...f,duration:0,width:0,height:0,status:'pending',proxyPath:'',proxySize:0,tcTime:0,objectKey:'',error:''}));
  }catch(err){log(`[diag] scan error=${err}`);log(`❌ 扫描失败: ${err}`)}
  S.processing=false;S.phase='idle';render();
}

async function doTranscode(){
  if(!canTranscode())return;
  S.processing=true;S.phase='transcoding';S.totalUploaded=0;S.totalSkipped=0;S.totalFailed=0;S.taskId='';S.taskUrl='';
  S.files.forEach(f=>{f.status='pending';f.proxyPath='';f.proxySize=0;f.tcTime=0;f.objectKey='';f.error=''});
  render();

  const okFiles=[];
  S.phase='probing';render();
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

  for(let j=0;j<okFiles.length;j++){
    const i=okFiles[j];const f=S.files[i];
    const stem=f.filename.replace(/\.[^.]+$/,'').replace(/[^a-zA-Z0-9_-]/g,'_');
    const proxyPath=`${S.outputDir}/proxy_${String(j).padStart(4,'0')}_${stem}.mp4`;
    S.phase='transcoding';S.currentIdx=i;f.status='transcoding';render();
    log(`🎬 转码 ${j+1}/${okFiles.length}: ${f.filename}`);
    try{
      const tc=await invoke('transcode_video',{inputPath:f.path,outputPath:proxyPath});
      if(!tc.ok){f.status='tc_failed';f.error=tc.error;S.totalFailed++;log(`  ❌ 转码失败: ${tc.error}`);render();continue}
      f.proxyPath=tc.proxy_path;f.proxySize=tc.proxy_size;f.tcTime=tc.time_secs;f.status='transcoded';log(`  ✅ 转码完成 ${tc.time_secs.toFixed(1)}s → ${fmtSize(tc.proxy_size)}`);
    }catch(err){f.status='tc_failed';f.error=String(err);S.totalFailed++;log(`  ❌ ${err}`);render();continue}
    render();
  }

  S.phase='done';S.processing=false;S.currentIdx=-1;
  log(`\n🎉 转码完成：${okFiles.length} 成功，${S.totalSkipped} 跳过，${S.totalFailed} 失败`);
  render();
}

async function doUpload(){
  if(!S.taskId&&!S.files.some(f=>f.status==='transcoded')){log('❌ 请先转码');return}
  S.processing=true;S.phase='uploading';S.totalUploaded=0;S.totalSkipped=0;S.totalFailed=0;
  const transcodedFiles=S.files.map((f,i)=>({...f,origIdx:i})).filter(f=>f.status==='transcoded');
  if(!transcodedFiles.length){log('⚠️ 没有可上传的文件');S.processing=false;render();return}

  // task/init
  log(`📡 创建任务 (${transcodedFiles.length} 个文件)…`);
  const filenames=transcodedFiles.map(f=>f.filename);
  try{
    const init=await invoke('task_init',{serverUrl:S.serverUrl,fileCount:transcodedFiles.length,filenames,videoTheme:S.videoTheme,newsEvent:S.newsEvent});
    if(!init.ok){S.phase='failed';S.processing=false;log(`❌ 创建任务失败: ${init.error}`);render();return}
    S.taskId=init.task_id;S.taskUrl=init.task_url;log(`✅ 任务创建成功: ${init.task_id}`);
  }catch(err){S.phase='failed';S.processing=false;log(`❌ ${err}`);render();return}

  const uploadedKeys=[];
  for(let j=0;j<transcodedFiles.length;j++){
    const f=transcodedFiles[j];const i=f.origIdx;
    S.currentIdx=i;S.files[i].status='uploading';render();
    log(`📤 上传 ${j+1}/${transcodedFiles.length}: ${f.filename}`);
    const proxyFn=f.proxyPath.split('/').pop();
    try{
      const up=await invoke('upload_file',{serverUrl:S.serverUrl,taskId:S.taskId,proxyPath:f.proxyPath,filename:proxyFn});
      if(!up.ok){S.files[i].status='failed';S.files[i].error=up.error;S.totalFailed++;log(`  ❌ 上传失败: ${up.error}`);render();continue}
      S.files[i].status='uploaded';S.files[i].objectKey=up.object_key;S.totalUploaded++;uploadedKeys.push(up.object_key);log(`  ✅ PUT 200 → ${up.object_key.split('/').pop()}`);
    }catch(err){S.files[i].status='failed';S.files[i].error=String(err);S.totalFailed++;log(`  ❌ ${err}`);render();continue}
    render();
  }

  if(uploadedKeys.length>0){
    S.phase='notifying';render();
    log(`📡 通知服务器 (${uploadedKeys.length} 个文件)…`);
    try{const n=await invoke('task_notify',{serverUrl:S.serverUrl,taskId:S.taskId,tosKeys:uploadedKeys,fileCount:uploadedKeys.length});if(n.ok)log(`✅ notify: ${n.status}`);else log(`⚠️ notify: ${n.error}`)}catch(err){log(`⚠️ notify 失败: ${err}`)}
  }

  S.phase='done';S.processing=false;S.currentIdx=-1;
  log(`\n🎉 上传完成：${S.totalUploaded} 成功，${S.totalSkipped} 跳过，${S.totalFailed} 失败`);
  if(S.taskUrl){log(`🌐 正在打开 Web 工作台…`);setTimeout(()=>doOpenUrl(S.taskUrl),1500)}
  render();
}

async function doOpenUrl(u){try{await shellOpen(u)}catch{window.open(u,'_blank')}}
async function toggleDiag(){S.showDiag=!S.showDiag;if(S.showDiag&&!S.diagInfo){try{S.diagInfo=await invoke('get_diag_info')}catch{}}render()}

function bind(){
  const t=document.getElementById('i-theme');if(t)t.onchange=ev=>{S.videoTheme=ev.target.value;sc({videoTheme:S.videoTheme})};
  const s=document.getElementById('i-event');if(s)s.oninput=ev=>{S.newsEvent=ev.target.value;sc({newsEvent:S.newsEvent})};
  // 所有按钮通过 JS 绑定，不依赖 inline onclick（避免 CSP 拦截）
  const bi=document.getElementById('btn-browse-input');if(bi)bi.onclick=doBrowseInput;
  const bo=document.getElementById('btn-browse-output');if(bo)bo.onclick=doBrowseOutput;
  const bs=document.getElementById('btn-scan');if(bs)bs.onclick=doScan;
  const bt=document.getElementById('btn-transcode');if(bt)bt.onclick=doTranscode;
  const bu=document.getElementById('btn-upload');if(bu)bu.onclick=doUpload;
}


// 最小诊断：启动时测试 fetch 并打印详细日志
async function diagFetch(){
  const baseUrl=S.serverUrl;
  log(`[diag] origin=${window.location.origin}`);
  log(`[diag] serverUrl=${baseUrl}`);
  log(`[diag] ua=${navigator.userAgent}`);
  const tests=[
    {url:`${baseUrl}/api/version`,label:'version'},
    {url:`${baseUrl}/api/health`,label:'health'},
  ];
  for(const t of tests){
    try{
      log(`[diag] fetch ${t.label}: ${t.url} ...`);
      const r=await fetch(t.url);
      log(`[diag] ${t.label}: status=${r.status} ok=${r.ok}`);
      log(`[diag] ${t.label}: headers=${[...r.headers.entries()].map(e=>e.join('=')).join('; ')}`);
      const txt=await r.text();
      log(`[diag] ${t.label}: body(${txt.length}B) ${txt.slice(0,80)}`);
    }catch(e){
      log(`[diag] ${t.label}: FAIL name=${e.name} msg=${e.message}`);
      log(`[diag] ${t.label}: stack=${e.stack||'(none)'}`);
    }
  }
}

function init(){const c=lc();let u=c.serverUrl||'http://47.93.194.154';if(u&&u.includes(':8088')){u='http://47.93.194.154';sc({serverUrl:u});log('[diag] cleared :8088 from cached serverUrl');}S.serverUrl=u;S.videoTheme=c.videoTheme||'';S.inputDir=c.inputDir||'';S.outputDir=c.outputDir||'/tmp/openclaw_uploader_proxy';S.newsEvent='';log('[diag] init: serverUrl='+S.serverUrl+' inputDir='+(S.inputDir||'(none)'));render();diagFetch();checkClientVersion()}

// ============================================================
// 客户端版本检查（v13.0 新增）
// ============================================================
function parseVer(v){
  const s=String(v).replace(/^v/,'');
  let m=s.match(/(\d+)\.(\d+)\.(\d+)/);
  if(m)return{major:+m[1],minor:+m[2],patch:+m[3]};
  m=s.match(/(\d+)\.(\d+)/);
  if(m)return{major:+m[1],minor:+m[2],patch:0};
  return{major:0,minor:0,patch:0};
}
function verLt(a,b){
  const A=parseVer(a),B=parseVer(b);
  if(A.major!==B.major)return A.major<B.major;
  if(A.minor!==B.minor)return A.minor<B.minor;
  return A.patch<B.patch;
}
async function checkClientVersion(){
  const url=S.serverUrl||'http://47.93.194.154';
  try{
    const resp=await fetch(`${url}/api/version`);
    if(!resp.ok)return;
    const data=await resp.json();
    if(data.status!=='ok')return;
    S.serverVersion=data.server_version||''; // v13.0.1: 保存服务端版本号
    const minVer=data.min_client_version;
    const recVer=data.recommended_client_version;
    if(minVer&&verLt(CLIENT_VERSION,minVer)){
      const downloadUrl=data.download?.macos||data.download?.windows||'';
      let msg=`当前上传助手版本过旧\n\n当前版本：v${CLIENT_VERSION}\n服务器最低要求：v${minVer}\n\n请下载最新版本后继续使用。`;
      if(downloadUrl)msg+=`\n\n下载地址：\n${downloadUrl}`;
      alert(msg);
      log(`❌ 客户端版本过旧 (v${CLIENT_VERSION} < 最低 v${minVer})，已阻止上传`);
      S.healthOk=false;S.healthMsg=`版本过旧 (需 ≥ v${minVer})`;render();
    }else if(recVer&&verLt(CLIENT_VERSION,recVer)){
      log(`⚠️ 建议升级到 v${recVer} (当前 v${CLIENT_VERSION})`);
    }else{
      log(`✅ 客户端版本正常 (v${CLIENT_VERSION})`);
    }
    render(); // 重新渲染以显示服务端版本号
  }catch(err){
    log(`⚠️ 无法获取服务器版本信息: ${err}，允许继续`);
  }
}
window.doBrowseInput=doBrowseInput;window.doBrowseOutput=doBrowseOutput;window.doHealth=doHealth;window.doFfmpeg=doFfmpeg;window.doScan=doScan;window.doTranscode=doTranscode;window.doUpload=doUpload;window.doOpenUrl=doOpenUrl;window.toggleDiag=toggleDiag;
init();
