// 元泉智影上传助手 — Tauri 主进程入口
// Phase 2B-2: 多文件串行转码上传队列

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::Command;
use serde::{Deserialize, Serialize};

fn resolve_ffmpeg(name: &str) -> String {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let s = dir.join(name);
            if s.exists() { return s.to_string_lossy().to_string(); }
            let b = dir.join("../Resources/bin").join(name);
            if b.exists() { return b.canonicalize().unwrap_or(b).to_string_lossy().to_string(); }
        }
    }
    name.to_string()
}

// ============================================================
// FFmpeg 检测
// ============================================================
#[derive(Serialize)]
struct FfmpegInfo { available: bool, path: String, version: String, error: String }

#[tauri::command]
fn detect_ffmpeg() -> (FfmpegInfo, FfmpegInfo) {
    let mk = |n: &str| {
        let p = resolve_ffmpeg(n);
        match Command::new(&p).arg("-version").output() {
            Ok(o) => FfmpegInfo { available: o.status.success(), path: p, version: String::from_utf8_lossy(&o.stdout).lines().next().unwrap_or("").into(), error: String::new() },
            Err(e) => FfmpegInfo { available: false, path: p, version: String::new(), error: e.to_string() }
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
    let mut hidden = 0usize;
    let mut small = 0usize;

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
        Ok(out) => VideoInfo { success:false, filename, path: path.clone(), duration:0.0, width:0, height:0, codec:String::new(), fps:String::new(), size:file_size, audio_codec:String::new(), status:"error".into(), status_reason:"ffprobe失败".into(), error:String::from_utf8_lossy(&out.stderr).into() },
        Err(e) => VideoInfo { success:false, filename, path: path.clone(), duration:0.0, width:0, height:0, codec:String::new(), fps:String::new(), size:file_size, audio_codec:String::new(), status:"error".into(), status_reason:"ffprobe不可用".into(), error:e.to_string() },
    }
}

// ============================================================
// 多文件串行全链路
// ============================================================
#[derive(Serialize)]
struct FileResult {
    filename: String, status: String,
    probe_ok: bool, probe_reason: String,
    transcode_ok: bool, transcode_time: f64, proxy_path: String, proxy_size: u64,
    upload_ok: bool, object_key: String, put_status: u16,
    error: String,
}

#[derive(Serialize)]
struct BatchUploadResult {
    task_id: String, task_url: String,
    total: usize, ok_count: usize, skipped: usize, failed: usize, uploaded: usize,
    notify_ok: bool, notify_status: String,
    files: Vec<FileResult>,
    overall_success: bool,
}

#[derive(Deserialize)] struct InitResp { task_id: Option<String>, task_url: Option<String>, error: Option<String> }
#[derive(Deserialize)] struct PresignResp { success: Option<bool>, put_url: Option<String>, object_key: Option<String>, error: Option<String> }
#[derive(Deserialize)] struct NotifyResp { status: Option<String>, error: Option<String> }

#[tauri::command]
fn batch_upload(server_url: String, folder: String, video_theme: String, news_event: String) -> BatchUploadResult {
    let base = server_url.trim_end_matches('/');
    let ffmpeg = resolve_ffmpeg("ffmpeg");
    let proxy_dir = "/tmp/openclaw_uploader_proxy";
    std::fs::create_dir_all(proxy_dir).ok();

    // 1. 扫描
    let scan = scan_folder(folder);
    let mut file_results: Vec<FileResult> = Vec::new();

    // 2. Probe 所有
    let mut ok_files: Vec<VideoInfo> = Vec::new();
    let mut skipped = 0usize;

    for sf in &scan.files {
        let info = probe_video(sf.path.clone());
        if info.status == "ok" {
            ok_files.push(info);
        } else {
            skipped += 1;
            file_results.push(FileResult {
                filename: sf.filename.clone(), status: format!("skipped: {}", info.status_reason),
                probe_ok: false, probe_reason: info.status_reason, transcode_ok: false, transcode_time: 0.0,
                proxy_path: String::new(), proxy_size: 0, upload_ok: false, object_key: String::new(),
                put_status: 0, error: info.error,
            });
        }
    }

    if ok_files.is_empty() {
        return BatchUploadResult {
            task_id: String::new(), task_url: String::new(),
            total: scan.total, ok_count: 0, skipped, failed: 0, uploaded: 0,
            notify_ok: false, notify_status: "no_ok_files".into(),
            files: file_results, overall_success: false,
        };
    }

    // 3. task/init
    let filenames: Vec<String> = ok_files.iter().map(|f| f.filename.clone()).collect();
    let init_body = serde_json::json!({
        "file_count": ok_files.len(),
        "filenames": filenames,
        "task_context": { "video_theme": &video_theme, "news_event": &news_event, "source": "macos-uploader-phase2b2" }
    });

    let init_resp = ureq::post(&format!("{}/api/ui/task/init", base))
        .set("Content-Type", "application/json")
        .send_string(&init_body.to_string());

    let (task_id, task_url) = match init_resp {
        Ok(r) => {
            let body = r.into_string().unwrap_or_default();
            match serde_json::from_str::<InitResp>(&body) {
                Ok(d) if d.task_id.is_some() => (d.task_id.unwrap(), d.task_url.unwrap_or_default()),
                _ => {
                    return BatchUploadResult { task_id: String::new(), task_url: String::new(), total: scan.total, ok_count: ok_files.len(), skipped, failed: ok_files.len(), uploaded: 0, notify_ok: false, notify_status: "init_failed".into(), files: file_results, overall_success: false };
                }
            }
        }
        Err(e) => {
            return BatchUploadResult { task_id: String::new(), task_url: String::new(), total: scan.total, ok_count: ok_files.len(), skipped, failed: ok_files.len(), uploaded: 0, notify_ok: false, notify_status: format!("init_error: {}", e), files: file_results, overall_success: false };
        }
    };

    // 4. 串行转码 + 上传
    let mut uploaded_keys: Vec<String> = Vec::new();
    let mut failed = 0usize;

    for (i, info) in ok_files.iter().enumerate() {
        let stem = Path::new(&info.path).file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or("v".into());
        let safe: String = stem.chars().map(|c| if c.is_alphanumeric() || c=='-' || c=='_' { c } else { '_' }).collect();
        let proxy_path = format!("{}/proxy_{:04}_{}.mp4", proxy_dir, i, safe);

        // 4a. 转码
        let t0 = std::time::Instant::now();
        let tc = Command::new(&ffmpeg).args([
            "-y","-i",&info.path,
            "-vf","scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-c:v","libx264","-preset","fast","-b:v","3M",
            "-r","25","-pix_fmt","yuv420p","-movflags","+faststart",
            "-c:a","aac","-b:a","128k",&proxy_path,
        ]).output();
        let tc_time = t0.elapsed().as_secs_f64();

        let tc_ok = tc.as_ref().map(|o| o.status.success()).unwrap_or(false);
        let proxy_size = if tc_ok { std::fs::metadata(&proxy_path).map(|m| m.len()).unwrap_or(0) } else { 0 };

        if !tc_ok {
            let err = tc.map(|o| String::from_utf8_lossy(&o.stderr).lines().rev().take(3).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n")).unwrap_or_else(|e| e.to_string());
            failed += 1;
            file_results.push(FileResult { filename: info.filename.clone(), status: "transcode_failed".into(), probe_ok: true, probe_reason: String::new(), transcode_ok: false, transcode_time: tc_time, proxy_path: String::new(), proxy_size: 0, upload_ok: false, object_key: String::new(), put_status: 0, error: err });
            continue;
        }

        // 4b. presign-put
        let proxy_fn = Path::new(&proxy_path).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or(info.filename.clone());
        let ps_body = serde_json::json!({ "task_id": &task_id, "filename": &proxy_fn, "content_type": "video/mp4", "file_size": proxy_size });
        let ps_resp = ureq::post(&format!("{}/api/ui/upload/presign-put", base))
            .set("Content-Type", "application/json")
            .send_string(&ps_body.to_string());

        let (put_url, obj_key) = match ps_resp {
            Ok(r) => {
                let body = r.into_string().unwrap_or_default();
                match serde_json::from_str::<PresignResp>(&body) {
                    Ok(d) if d.success.unwrap_or(false) => (d.put_url.unwrap_or_default(), d.object_key.unwrap_or_default()),
                    Ok(d) => { failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"presign_failed".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path:proxy_path.clone(), proxy_size, upload_ok:false, object_key:String::new(), put_status:0, error:d.error.unwrap_or("presign fail".into()) }); continue; }
                    Err(e) => { failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"presign_error".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path:proxy_path.clone(), proxy_size, upload_ok:false, object_key:String::new(), put_status:0, error:e.to_string() }); continue; }
                }
            }
            Err(e) => { failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"presign_error".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path:proxy_path.clone(), proxy_size, upload_ok:false, object_key:String::new(), put_status:0, error:e.to_string() }); continue; }
        };

        // 4c. PUT
        let file_data = match std::fs::read(&proxy_path) {
            Ok(d) => d,
            Err(e) => { failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"read_failed".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path:proxy_path.clone(), proxy_size, upload_ok:false, object_key:obj_key.clone(), put_status:0, error:e.to_string() }); continue; }
        };

        let put_resp = ureq::put(&put_url).set("Content-Type", "video/mp4").send_bytes(&file_data);
        match put_resp {
            Ok(r) if r.status() == 200 => {
                uploaded_keys.push(obj_key.clone());
                file_results.push(FileResult { filename:info.filename.clone(), status:"uploaded".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path, proxy_size, upload_ok:true, object_key:obj_key, put_status:200, error:String::new() });
            }
            Ok(r) => { let st=r.status(); let body=r.into_string().unwrap_or_default(); failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"put_failed".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path, proxy_size, upload_ok:false, object_key:obj_key, put_status:st, error:body }); }
            Err(e) => { failed+=1; file_results.push(FileResult { filename:info.filename.clone(), status:"put_error".into(), probe_ok:true, probe_reason:String::new(), transcode_ok:true, transcode_time:tc_time, proxy_path, proxy_size, upload_ok:false, object_key:obj_key, put_status:0, error:e.to_string() }); }
        }
    }

    // 5. notify
    let uploaded = uploaded_keys.len();
    let (notify_ok, notify_status) = if uploaded > 0 {
        let nb = serde_json::json!({ "tos_keys": &uploaded_keys, "file_count": uploaded });
        match ureq::post(&format!("{}/api/ui/task/{}/notify", base, task_id)).set("Content-Type","application/json").send_string(&nb.to_string()) {
            Ok(r) => {
                let body = r.into_string().unwrap_or_default();
                match serde_json::from_str::<NotifyResp>(&body) {
                    Ok(d) => { let st = d.status.unwrap_or("unknown".into()); (st == "processing", st) }
                    Err(_) => (false, "parse_error".into())
                }
            }
            Err(e) => (false, e.to_string())
        }
    } else {
        (false, "no_uploads".into())
    };

    BatchUploadResult {
        task_id, task_url, total: scan.total, ok_count: ok_files.len(),
        skipped, failed, uploaded, notify_ok, notify_status,
        files: file_results, overall_success: notify_ok && failed == 0,
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            detect_ffmpeg, probe_video, scan_folder, batch_upload,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
