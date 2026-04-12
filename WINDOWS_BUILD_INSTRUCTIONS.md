# Windows 程序打包说明

## 方法 1：GitHub Actions（推荐）

### 步骤

1. **将代码推送到 GitHub**
   ```bash
   cd /tmp/video-tool-test-48975
   git init
   git add .
   git commit -m "Add Windows build workflow"
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

2. **在 GitHub 上触发构建**
   - 访问仓库的 Actions 标签
   - 选择 "Build Windows Executable" workflow
   - 点击 "Run workflow"
   - 等待构建完成（约 5-10 分钟）

3. **下载产物**
   - 构建完成后，点击最新的 workflow run
   - 在页面底部找到 "Artifacts"
   - 点击 `windows-package` 下载
   - 或等待自动上传到 TOS（如果配置了凭据）

### 配置 TOS 凭据（可选）

在 GitHub 仓库的 Settings → Secrets and variables → Actions 中添加：

- `TOS_AK`: `AKLTZGIxYjcxZWY2NzIyNDIxYjhiOGZjMTYzY2E4OGQxYzE`
- `TOS_SK`: `T0RFMVlUSXdZbUl3WVdVMU5HVTBPV0kyTURWak5UYzROemd5TkdWaU9UWQ==`

---

## 方法 2：本地 Windows 环境

### 前提条件

- Windows 10/11
- Python 3.8+ 已安装
- 已勾选 "Add Python to PATH"

### 步骤

1. **复制项目到 Windows**
   ```
   复制整个 windows_ingest_helper/ 目录到 Windows 电脑
   ```

2. **运行打包脚本**
   ```
   双击 build_windows.bat
   ```

3. **等待打包完成**
   - 自动安装依赖（pyinstaller, tos）
   - 打包为 EXE
   - 创建发布包
   - 压缩为 ZIP

4. **产物位置**
   ```
   windows_ingest_helper/Windows_Ingest_Helper_v2.zip
   ```

---

## 产物说明

### Windows_Ingest_Helper_v2.zip

**内容**:
```
Windows_Release/
├── ingest_helper.exe     # 主程序（双击运行）
├── upload_to_tos.py      # TOS 上传工具
├── README.txt            # 使用说明
└── version.txt           # 版本信息
```

**大小**: 约 20-30 MB（包含 PyInstaller 运行时）

**启动方式**:
1. 解压 ZIP
2. 双击 `ingest_helper.exe`
3. GUI 界面自动打开

---

## 当前可用版本

如果暂时无法使用上述方法，可先使用当前 Python 源码包：

**下载链接**: https://e23-video.tos-cn-beijing.volces.com/Windows_Run_Package/Windows_Run_Package.zip

**前提**: 需要已安装 Python 3.8+

**使用方式**:
1. 解压
2. 双击 `start.bat`
3. 程序自动检测 Python 并安装依赖

---

## TOS 上传配置

已硬编码在程序中：
- Bucket: `e23-video`
- Region: `cn-beijing`
- Endpoint: `tos-cn-beijing.volces.com`

如需上传功能，设置环境变量：
```bash
set TOS_AK=你的 Access Key
set TOS_SK=你的 Secret Key
```
