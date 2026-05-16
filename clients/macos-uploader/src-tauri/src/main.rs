// 元泉智影上传助手 — Tauri 主进程入口
// Phase 3G: Windows 对齐 + 实时进度（前端 orchestrator）

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::Path;
use std::process::Command;
use serde::{Deserialize, Serialize};

fn resolve_ffmpeg(name: &str) -> String {
    // 1. App Bundle sidecar（优先）
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let sidecar = dir.join(name);
            if sidecar.exists() { return sidecar.to_string_lossy().to_string(); }
            let res = dir.join("../Resources/bin").join(name);
            if res.exists() { return res.canonicalize().unwrap_or(res).to_string_lossy().to_string(); }
        }
    }
    // 2. Homebrew fallback
    for p in ["/opt/homebrew/bin", "/usr/local/bin"] {
        let full = format!("{}/{}", p, name);
        if std::path::Path::new(&full).exists() { return full; }
    }
    name.to_string()
}

// ============================================================
// Health（Rust 侧，绕过 WebView HTTP 限制）
// ============================================================
#[derive(Serialize)]
struct HealthResult { ok: bool, status: u16, version: String, error: String, url: String }

#[tauri::command]
fn check_health(server_url: String) -> HealthResult {
    let url = format!("{}/api/health", server_url.trim_end_matches('/'));
    match ureq::get(&url).timeout(std::time::Duration::from_secs(10)).call() {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.into_string().unwrap_or_default();
            let version = serde_json::from_str::<serde_json::Value>(&body)
                .ok().and_then(|v| v["version"].as_str().map(String::from)).unwrap_or_default();
            HealthResult { ok: status == 200, status, version, error: String::new(), url }
        }
        Err(e) => HealthResult { ok: false, status: 0, version: String::new(), error: e.to_string(), url }
    }
}

// ============================================================
// 诊断
// ============================================================
#[derive(Serialize)]
struct DiagInfo { app_version: String, exe_path: String, ffmpeg_path: String, ffprobe_path: String, proxy_dir: String }

#[tauri::command]
fn get_diag_info() -> DiagInfo {
    DiagInfo {
        app_version: "0.1.0-alpha (Phase 3G)".into(),
        exe_path: std::env::current_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or("unknown".into()),
        ffmpeg_path: resolve_ffmpeg("ffmpeg"),
        ffprobe_path: resolve_ffmpeg("ffprobe"),
        proxy_dir: "/tmp/openclaw_uploader_proxy".into(),
    }
}

// ============================================================
// FFmpeg 检测
// ============================================================
#[derive(Serialize)]
struct FfmpegInfo { available: bool, path: String, version: String, error: String, exists: bool, executable: bool }

#[tauri::command]
fn detect_ffmpeg() -> (FfmpegInfo, FfmpegInfo) {
    let mk = |n: &str| {
        let p = resolve_ffmpeg(n);
        let exists = std::path::Path::new(&p).exists();
        let executable = exists && {
            #[cfg(unix)]
            { use std::os::unix::fs::PermissionsExt; std::fs::metadata(&p).map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false) }
            #[cfg(not(unix))]
            { true }
        };
        if !exists {
            let err = format!("文件不存在: {}", &p);
            return FfmpegInfo { available: false, path: p, version: String::new(), error: err, exists, executable };
        }
        if !executable {
            let err = format!("无执行权限: {}", &p);
            return FfmpegInfo { available: false, path: p, version: String::new(), error: err, exists, executable };
        }
        match Command::new(&p).arg("-version").output() {
            Ok(o) if o.status.success() => {
                let ver = String::from_utf8_lossy(&o.stdout).lines().next().unwrap_or("").to_string();
                FfmpegInfo { available: true, path: p, version: ver, error: String::new(), exists, executable }
            }
            Ok(o) => {
                let stderr = String::from_utf8_lossy(&o.stderr);
                FfmpegInfo { available: false, path: p, version: String::new(), error: format!("exit {}: {}", o.status.code().unwrap_or(-1), stderr.chars().take(200).collect::<String>()), exists, executable }
            }
            Err(e) => FfmpegInfo { available: false, path: p, version: String::new(), error: e.to_string(), exists, executable }
        }
    };
    (mk("ffmpeg"), mk("ffprobe"))
}

// ============================================================
// 扫描目录
// ============================================================
#[derive(Serialize)]
struct ScannedFile { path: String, filename: String, size: u64 }

#[derive(Serialize)]
struct ScanResult { files: Vec<ScannedFile>, total: usize, skipped_hidden: usize, skipped_small: usize }

#[tauri::command]
fn scan_folder(folder: String) -> ScanResult {
    let exts = ["mp4","mov","m4v","mkv","avi"];
    let mut files = Vec::new();
    let (mut hidden, mut small) = (0usize, 0usize);
    if let Ok(entries) = std::fs::read_dir(&folder) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_file() { continue; }
            let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            if name.starts_with('.') { hidden += 1; continue; }
            let ext = path.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
            if !exts.contains(&ext.as_str()) { continue; }
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            if size < 102400 { small += 1; continue; }
            files.push(ScannedFile { path: path.to_string_lossy().into(), filename: name, size });
        }
    }
    files.sort_by(|a, b| a.filename.cmp(&b.filename));
    let total = files.len();
    ScanResult { files, total, skipped_hidden: hidden, skipped_small: small }
}

// ============================================================
// Probe 单文件
// ============================================================
#[derive(Serialize, Clone)]
struct VideoInfo {
    success: bool, filename: String, path: String, duration: f64, width: u32, height: u32,
    codec: String, fps: String, size: u64, audio_codec: String,
    status: String, status_reason: String, error: String,
}

#[tauri::command]
fn probe_video(path: String) -> VideoInfo {
    let ffprobe = resolve_ffmpeg("ffprobe");
    let p = Path::new(&path);
    let filename = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let file_size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    let result = Command::new(&ffprobe)
        .args(["-v","error","-show_entries","stream=width,height,duration,r_frame_rate,codec_name,codec_type",
               "-show_entries","format=duration","-of","json",&path]).output();
    match result {
        Ok(out) if out.status.success() => {
            let json: serde_json::Value = serde_json::from_str(&String::from_utf8_lossy(&out.stdout)).unwrap_or_default();
            let (mut w,mut h,mut dur,mut codec,mut fps,mut ac) = (0u32,0u32,0.0f64,String::new(),String::new(),String::new());
            if let Some(streams) = json["streams"].as_array() {
                for s in streams {
                    let ct = s["codec_type"].as_str().unwrap_or("");
                    if ct=="video" && w==0 { w=s["width"].as_u64().unwrap_or(0) as u32; h=s["height"].as_u64().unwrap_or(0) as u32; codec=s["codec_name"].as_str().unwrap_or("").into(); fps=s["r_frame_rate"].as_str().unwrap_or("").into(); dur=s["duration"].as_str().and_then(|d|d.parse().ok()).unwrap_or(0.0); }
                    if ct=="audio" && ac.is_empty() { ac=s["codec_name"].as_str().unwrap_or("").into(); }
                }
            }
            if dur<=0.0 { dur=json["format"]["duration"].as_str().and_then(|d|d.parse().ok()).unwrap_or(0.0); }
            let (st,sr) = if dur<=1.5 { ("skip_short".into(), format!("时长过短({:.1}s)",dur)) }
                else if h>0 && h<360 { ("bad_resolution".into(), format!("分辨率过低({}x{})",w,h)) }
                else { ("ok".into(), String::new()) };
            VideoInfo { success:true, filename, path: path.clone(), duration:dur, width:w, height:h, codec, fps, size:file_size, audio_codec:ac, status:st, status_reason:sr, error:String::new() }
        }
        Ok(out) => VideoInfo { success:false, filename, path: path.clone(), duration:0.0, width:0, height:0, codec:String::new(), fps:String::new(), size:file_size, audio_codec:String::new(), status:"error".into(), status_reason:"ffprobe 失败".into(), error:String::from_utf8_lossy(&out.stderr).chars().take(300).collect() },
        Err(e) => VideoInfo { success:false, filename, path: path.clone(), duration:0.0, width:0, height:0, codec:String::new(), fps:String::new(), size:file_size, audio_codec:String::new(), status:"error".into(), status_reason:"ffprobe 不可用".into(), error:e.to_string() },
    }
}

// ============================================================
// 单文件转码（前端 orchestrator 逐个调用）
// ============================================================
#[derive(Serialize)]
struct TranscodeResult { ok: bool, proxy_path: String, proxy_size: u64, time_secs: f64, error: String }

#[tauri::command]
fn transcode_video(input_path: String, output_path: String) -> TranscodeResult {
    let ffmpeg = resolve_ffmpeg("ffmpeg");
    std::fs::create_dir_all(Path::new(&output_path).parent().unwrap_or(Path::new("/tmp"))).ok();
    let t0 = std::time::Instant::now();
    let tc = Command::new(&ffmpeg).args([
        "-y","-i",&input_path,
        "-vf","scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-c:v","libx264","-preset","fast","-b:v","3M",
        "-r","25","-pix_fmt","yuv420p","-movflags","+faststart",
        "-c:a","aac","-b:a","128k",&output_path,
    ]).output();
    let time_secs = t0.elapsed().as_secs_f64();
    match tc {
        Ok(o) if o.status.success() => {
            let proxy_size = std::fs::metadata(&output_path).map(|m| m.len()).unwrap_or(0);
            TranscodeResult { ok: true, proxy_path: output_path, proxy_size, time_secs, error: String::new() }
        }
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr);
            let last3: String = err.lines().rev().take(3).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n");
            TranscodeResult { ok: false, proxy_path: output_path, proxy_size: 0, time_secs, error: last3 }
        }
        Err(e) => TranscodeResult { ok: false, proxy_path: output_path, proxy_size: 0, time_secs, error: e.to_string() }
    }
}

// ============================================================
// task/init
// ============================================================
#[derive(Serialize)]
struct TaskInitResult { ok: bool, task_id: String, task_url: String, error: String }

#[derive(Deserialize)]
struct InitResp { task_id: Option<String>, task_url: Option<String>, #[allow(dead_code)] error: Option<String> }

#[tauri::command]
fn task_init(server_url: String, file_count: usize, filenames: Vec<String>, video_theme: String, news_event: String) -> TaskInitResult {
    let base = server_url.trim_end_matches('/');
    let body = serde_json::json!({
        "file_count": file_count, "filenames": filenames,
        "task_context": { "video_theme": &video_theme, "news_event": &news_event, "source": "macos-uploader-phase3g" }
    });
    match ureq::post(&format!("{}/api/ui/task/init", base)).set("Content-Type","application/json").send_string(&body.to_string()) {
        Ok(r) => {
            let b = r.into_string().unwrap_or_default();
            match serde_json::from_str::<InitResp>(&b) {
                Ok(d) if d.task_id.is_some() => TaskInitResult { ok: true, task_id: d.task_id.unwrap(), task_url: d.task_url.unwrap_or_default(), error: String::new() },
                _ => TaskInitResult { ok: false, task_id: String::new(), task_url: String::new(), error: "解析失败".into() }
            }
        }
        Err(e) => TaskInitResult { ok: false, task_id: String::new(), task_url: String::new(), error: e.to_string() }
    }
}

// ============================================================
// 单文件上传（presign + PUT）
// ============================================================
#[derive(Serialize)]
struct UploadResult { ok: bool, object_key: String, put_status: u16, error: String }

#[derive(Deserialize)]
struct PresignResp { success: Option<bool>, put_url: Option<String>, object_key: Option<String>, error: Option<String> }

#[tauri::command]
fn upload_file(server_url: String, task_id: String, proxy_path: String, filename: String) -> UploadResult {
    let base = server_url.trim_end_matches('/');
    let proxy_size = std::fs::metadata(&proxy_path).map(|m| m.len()).unwrap_or(0);

    // presign
    let ps_body = serde_json::json!({ "task_id": &task_id, "filename": &filename, "content_type": "video/mp4", "file_size": proxy_size });
    let ps_resp = ureq::post(&format!("{}/api/ui/upload/presign-put", base))
        .set("Content-Type","application/json").send_string(&ps_body.to_string());
    let (put_url, obj_key) = match ps_resp {
        Ok(r) => {
            let b = r.into_string().unwrap_or_default();
            match serde_json::from_str::<PresignResp>(&b) {
                Ok(d) if d.success.unwrap_or(false) => (d.put_url.unwrap_or_default(), d.object_key.unwrap_or_default()),
                Ok(d) => return UploadResult { ok: false, object_key: String::new(), put_status: 0, error: d.error.unwrap_or("presign 失败".into()) },
                Err(e) => return UploadResult { ok: false, object_key: String::new(), put_status: 0, error: e.to_string() }
            }
        }
        Err(e) => return UploadResult { ok: false, object_key: String::new(), put_status: 0, error: e.to_string() }
    };

    // PUT
    let file_data = match std::fs::read(&proxy_path) {
        Ok(d) => d,
        Err(e) => return UploadResult { ok: false, object_key: obj_key, put_status: 0, error: e.to_string() }
    };
    match ureq::put(&put_url).set("Content-Type","video/mp4").send_bytes(&file_data) {
        Ok(r) if r.status() == 200 => UploadResult { ok: true, object_key: obj_key, put_status: 200, error: String::new() },
        Ok(r) => { let st = r.status(); UploadResult { ok: false, object_key: obj_key, put_status: st, error: format!("HTTP {}", st) } }
        Err(e) => UploadResult { ok: false, object_key: obj_key, put_status: 0, error: e.to_string() }
    }
}

// ============================================================
// notify
// ============================================================
#[derive(Serialize)]
struct NotifyResult { ok: bool, status: String, error: String }

#[derive(Deserialize)]
struct NotifyResp { status: Option<String>, #[allow(dead_code)] error: Option<String> }

#[tauri::command]
fn task_notify(server_url: String, task_id: String, tos_keys: Vec<String>, file_count: usize) -> NotifyResult {
    let base = server_url.trim_end_matches('/');
    let body = serde_json::json!({ "tos_keys": &tos_keys, "file_count": file_count });
    match ureq::post(&format!("{}/api/ui/task/{}/notify", base, task_id)).set("Content-Type","application/json").send_string(&body.to_string()) {
        Ok(r) => {
            let b = r.into_string().unwrap_or_default();
            match serde_json::from_str::<NotifyResp>(&b) {
                Ok(d) => { let st = d.status.unwrap_or("unknown".into()); NotifyResult { ok: st == "processing", status: st, error: String::new() } }
                Err(_) => NotifyResult { ok: false, status: String::new(), error: "解析错误".into() }
            }
        }
        Err(e) => NotifyResult { ok: false, status: String::new(), error: e.to_string() }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            check_health, get_diag_info, detect_ffmpeg,
            scan_folder, probe_video, transcode_video,
            task_init, upload_file, task_notify,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
