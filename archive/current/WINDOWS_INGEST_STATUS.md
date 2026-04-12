# Windows 上传预处理工具状态

**最后更新**: 2026-04-12 22:20  
**当前有效版本**: v7

---

## 一、当前有效版本

| 项目 | 值 |
|------|-----|
| 版本 | v7 |
| 构建方式 | GitHub Actions windows-latest |
| Run ID | 24308155285 |
| 文件大小 | 80.4 MB |
| 下载链接 | https://e23-video.tos-cn-beijing.volces.com/Windows_Executable/Windows_Ingest_Helper_v7.zip |

---

## 二、真实验证结论

**验证环境**: 真实 Windows 环境

| 指标 | 数值 |
|------|------|
| 真实素材总数 | 47 条 |
| 成功读取 | 41 条 |
| 失败 | 6 条 |
| 成功率 | 87.2% |

**失败原因**:
- 个别"编辑软件导出的成片"存在兼容性读取问题
- 坏片/短片筛除逻辑已初步生效

---

## 三、当前已成立能力

| 能力 | 状态 | 说明 |
|------|------|------|
| Windows 程序启动 | ✅ 成立 | 双击 ingest_helper.exe 即可运行 |
| 目录选择 | ✅ 成立 | 可选择任意本地素材目录 |
| 素材扫描 | ✅ 成立 | 可扫描.mp4/.mov/.avi/.mkv/.m4v |
| ffprobe 元数据读取 | ✅ 成立 | 可读取 duration/width/height/fps/codec |
| 720p proxy 转码 | ✅ 成立 | ffmpeg 转码正常 |
| manifest.json 生成 | ✅ 成立 | 真实生成并保存 |
| GitHub Actions 打包 | ✅ 成立 | 可自动构建含 ffmpeg/ffprobe 的完整包 |

---

## 四、当前未完成能力

| 能力 | 状态 | 说明 |
|------|------|------|
| 个别成片兼容性 | ⚠️ 待排查 | 编辑软件导出的特殊格式 |
| TOS 真上传 | ⏳ 待接入 | TOS 配置已就绪，待程序接入 |
| tos_key / tos_url 回写 | ⏳ 待完成 | manifest 回写逻辑待实现 |

---

## 五、下一步动作

**允许推进**:
1. 个别特殊成片兼容性排查
2. TOS 真上传接入
3. manifest 回写 tos_key/tos_url

**禁止回头**:
- ❌ 禁止重新折腾 Windows 打包链
- ❌ 禁止重新折腾 GitHub Actions workflow
- ❌ 禁止重新折腾 ffmpeg 打包

---

## 六、版本历史

| 版本 | 状态 | 说明 |
|------|------|------|
| v3 | 历史 | 首次构建成功 |
| v4 | 历史 | 调试增强版（未真正修复） |
| v5 | ❌ 作废 | shim 假 ffmpeg 版本（384 KB） |
| v6 | 历史 | 真 ffmpeg/ffprobe 带入版本（96 MB） |
| v7 | ✅ 当前有效 | 修复 ffprobe JSON 解析并完成本地预处理闭环 |

---

**最后更新**: 2026-04-12 22:20
