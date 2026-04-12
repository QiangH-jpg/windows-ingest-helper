# 里程碑快照：主链稳定基线

**日期**: 2026-04-08  
**版本**: v3.5 (mainchain-stable)  
**状态**: 主链稳定可用

---

## 1. 架构说明（V1~V5）

```
V1 素材层 (v1_materials/material_pool.py)
  → uploads/materials/ 唯一来源
  → 标准化转码（h264, 1280x720, 25fps）
  → 缓存管理

V3 时序层 (v3_timeline/timeline_orchestrator.py)
  → 按素材顺序排列
  → 固定 3 秒/镜头
  → 总时长对齐音频

V3.5 裁剪层 (v3_timeline/clip_extractor.py)
  → 按 timeline 裁剪每个素材为子片段
  → 输出 data/clips/clip_*.mp4

V4 渲染层 (v4_render/render_engine.py)
  → TTS 生成
  → 字幕生成（短语保护）
  → 只拼接 clip_*.mp4

V5 校验层 (v5_gate/quality_gate.py)
  → 5 道门禁
  → 全部通过 → outputs/approved/
  → 任一失败 → data/rejection_report.json
```

---

## 2. 唯一主链流程

```
scripts/run_production.py
  ↓
V1 素材层 → 标准化素材
  ↓
V3 时序层 → timeline.json
  ↓
V3.5 裁剪层 → data/clips/clip_*.mp4
  ↓
V4 渲染层 → outputs/raw/candidate.mp4
  ↓
V5 校验层 → outputs/approved/*_approved.mp4
```

---

## 3. 关键模块列表

| 模块 | 文件 | 作用 |
|------|------|------|
| 素材池 | v1_materials/material_pool.py | 列出/标准化素材 |
| 时间轴 | v3_timeline/timeline_orchestrator.py | 生成 timeline.json |
| 裁剪器 | v3_timeline/clip_extractor.py | 裁剪子片段 |
| 渲染引擎 | v4_render/render_engine.py | TTS + 字幕 + 合成 |
| 质量门禁 | v5_gate/quality_gate.py | 5 道门禁校验 |
| 配置管理 | config/settings.py | 环境变量 + 路径配置 |
| 主链入口 | scripts/run_production.py | 唯一入口 |

---

## 4. 门禁规则摘要

| 门禁 | 规则 | 阻断条件 |
|------|------|----------|
| 1 | 全量 clip 时长 | 任一 clip < 1.5s |
| 2 | 字幕完整性 | 匹配率 < 90% |
| 3 | 结尾校验 | 视频提前 >0.5s 或拖尾 >3s |
| 4 | 时长合理性 | 超出 20-180 秒 |
| 5 | 文件大小 | < 5MB |

---

## 5. 旧链归档说明

- **归档位置**: legacy/
- **脚本数量**: 23 个旧 run_*.py 脚本
- **状态**: 已冻结，不再参与生成
- **复用模块**: pipeline/video_cache.py, pipeline/processor.py, pipeline/tts_provider.py

---

## 6. 当前能力范围

**已实现**:
- 真实素材视频生成
- TTS 配音
- 字幕渲染（按时间轴逐条显示）
- 短语保护（无词中间拆分）
- 音视频对齐（误差 <0.1s）
- 5 道门禁校验

**未实现（待开发）**:
- V2 语义选片（画面与稿件匹配）
- V3 节奏优化（短/中/长镜头分布）
- 多稿件支持
- Web 界面一键生成

---

## 7. 下一步规划

### 短期（本周）
1. 接入 V2 语义选片（基于标签匹配）
2. V3 节奏优化（短 30%/中 50%/长 20%）
3. Web 界面集成新主链

### 中期（下周）
1. 多稿件支持
2. 素材标签管理
3. 批量生成

### 长期（下月）
1. 清理 legacy/
2. 性能优化（并行处理）
3. 迁移到新服务器
