# 报告索引

**最后更新**: 2026-04-12  
**状态**: 样片相关报告进入阶段性封存

---

## 一、当前报告状态

**样片相关报告**: 阶段性封存  
**原因**: 下一阶段主线不再继续围绕单样片补丁修复推进

**已封存报告**:
- A/B 样片修复报告 v1-v8
- 问题镜头排查报告
- 尾帧 hold 问题修复报告

---

## 二、新阶段报告目录

下一阶段报告目录将转向：

### 1. 上传预处理
- Windows 上传助手开发日志
- 素材清单 JSON 规范
- 坏片检测规则

### 2. 原始素材粗识别
- 粗识别 JSON 输出规范
- 素材类型粗分规则
- 专访分流标准

### 3. 候选收缩
- 过滤规则
- 去重算法
- 低价值镜头定义

### 4. 专访分流
- 专访识别标准
- 分流池管理
- 专访审看流程

### 5. 精选深审
- AI 评分标准
- 排序算法
- 优选清单生成

### 6. 素材带优选
- 新闻稿驱动选片
- 时间线生成
- 成片输出

---

## 三、Windows 版本报告索引

| 版本 | 状态 | 说明 |
|------|------|------|
| v3 | 历史版本 | 正式包首次构建成功 |
| v4 | 历史版本 | 调试增强版（未真正修复） |
| v5 | ❌ 作废 | shim 假 ffmpeg 版本（384 KB，非真实二进制） |
| v6 | 历史版本 | 真 ffmpeg/ffprobe 带入版本（96 MB） |
| v7 | ✅ 当前有效 | 修复 ffprobe JSON 解析并完成本地预处理闭环验证 |

**v7 真实验证结果**:
- 真实素材总数：47 条
- 成功读取：41 条
- 失败：6 条
- 成功率：87.2%

---

## 四、报告保存位置

| 类型 | 路径 |
|------|------|
| 项目状态 | `archive/state/PROJECT_STATE.md` |
| 阶段边界 | `archive/current/NEXT_PHASE_BOUNDARY.md` |
| Windows 状态 | `archive/current/WINDOWS_INGEST_STATUS.md` |
| 阶段总结 | `archive/current/WINDOWS_V7_STAGE_SUMMARY.md` |
| 输入标准 | `archive/current/DOUBAO_VIDEO_INPUT_STANDARD.md` |
| 里程碑 | `archive/milestones/MILESTONES.md` |
| 阶段计划 | `archive/current/NEXT_STAGE_INGEST_AND_PREPROCESS_PLAN.md` |
| 日志 | `archive/hot/` `archive/daily/` |

---

**文档状态**: ✅ 已更新
