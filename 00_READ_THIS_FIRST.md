# 00_READ_THIS_FIRST.md — 项目状态总控页

> **每次开工、每次重启、每次继续执行前，必须先读这个文件。**
> 最后更新：2026-04-30 07:05

---

## A. 当前项目阶段

- **项目**：视频新闻自动化主链（新闻短视频 AI 组片）
- **阶段**：单样本验证通过 + 工作台迭代中
- **结论**：L2→L3→TTS→成片闭环已验证；工作台候选池/精选瞬间/失败闭环/video_theme 全链路已接入
- **当前样片**：v12_final 已阶段性通过（不再微调）
- **验收报告**：`docs/V12_STAGE_ACCEPTANCE.md`

---

## B. 当前唯一正式主链

| 组件 | 文件路径 | 版本 |
|------|---------|------|
| **render 入口** | `scripts/v9_fixed_render.py` | mainchain v9_fixed_2026-04-20 |
| **L2 prompt** | `prompts/video_news/l2_three_tier_review_prompt_v1.txt` | **v1.2.0** |
| **L3 prompt** | `prompts/video_news/l3_director_prompt_v7.txt` | **v7.2.0** |
| **L2 后处理** | `pipeline/combined_review.py` | — |
| **主链配置** | `pipeline/mainchain_config.py` | — |
| **TTS** | `pipeline/tts_volcengine.py` | voice=S_x249qIGO1 |
| **字幕链** | `pipeline/tts_provider.py` → `create_subtitle_srt_from_meta()` | 卡点字幕 |
| **字幕样式** | `pipeline/render_preflight_checks.py` → `get_subtitle_style()` | FontSize=24 纯白无描边 |
| **主片池** | `outputs/full_run/l2_clean_windows_full.json` | strong_safe only |
| **候选池 API** | `/api/ui/pool?view=windows` → L2 clean_windows | — |
| **精选瞬间 API** | `/api/ui/pool?view=highlights` → L2 best_moment_candidates | — |
| **工作台前端** | `webui/dist/task-workbench.html` | — |
| **工作台后端** | `app/main.py` | — |
| **EXE** | `v15-tasklink/main_gui.py` | **v3.2.3** |
| **GitHub** | `QiangH-jpg/windows-ingest-helper` | main 分支 |
| **Actions workflow** | `.github/workflows/build-v15-gui.yml` | artifact: v15-gui-v3.2-onedir |
| **人工覆盖** | `pipeline/pool_overrides.py` | — |

---

## C. 当前永久硬规则

1. `clean_windows` = strong_safe ONLY，weak_safe 只进 fallback
2. 脏边界（foreground_intrusion / partial_head_popin / entry_dirty / exit_dirty）由 L2 处理，dirty 段 strong→weak 强制降级，不得直接送进 timeline
3. L3 负责在干净候选中做主题优选（slot_candidates + theme/action 评分），不得越界取段
4. feedback / interaction 类镜头 ≤ 5.0s，non_core_activity ≤ 4.0s
5. 成片禁止 `-shortest`，用 tpad 补尾
6. 字幕走卡点字幕链（`create_subtitle_srt_from_meta`），不回退旧链
7. deprecated 旧脚本不得作为正式出片入口（列表见 `mainchain_config.py`）
8. obvious_shake → 强制 unsafe，slight_handheld → 强制 weak
9. **候选池与精选瞬间必须使用独立数据源**：windows 读 clean_windows，highlights 读 best_moment_candidates
10. **缩略图 fallback**：窗口缩略图 → 同素材第一窗口 → frames 素材级首帧（fallback 只是兜底）
11. **候选池 processing 必须支持 stalled / failed 失败闭环**（5分钟无更新→stalled，异常→failed）
12. **video_theme / news_event 必须进入 Flash / Pro 上下文**，不能只写进 task JSON

---

## D. 当前已验证通过的能力

- ✅ L2 三档审查 + 边界洁净度 + motion_type 强制映射
- ✅ L2 dirty_boundary 自动降级
- ✅ L3 slot_candidates 主题优选 + 采访时长纪律
- ✅ 定点精修不全量重跑
- ✅ L2/L3 职责边界固化
- ✅ TTS + 卡点字幕 + preflight 渲染校验
- ✅ 字幕分词 jieba 重构（v10.3）+ 永久固化（v10.4）：双字词 fallback 已废弃，validate_subtitles_no_split() 在烧录前强制自检
- ✅ 工作台原始素材分析区真实递增
- ✅ 候选池 task_id 隔离 + 不抢跑（未完成前返回空态）
- ✅ video_theme / news_event 全链路（EXE→JSON→context→Flash→Pro→产物）
- ✅ 候选池失败闭环（stalled / failed 检测）
- ✅ 候选池与精选瞬间独立数据源
- ✅ 可拖拽时间轴锚点 + 人工干预写回
- ✅ EXE 中文路径兼容 + 启动自检 + TOS 上传重试

---

## E. 当前已知技术债

1. 模型对 obvious_shake / slight_handheld 判定仍有波动，靠映射规则止血
2. window_index → start_sec/end_sec 转换已加自动修复，但模型行为未根治
3. 候选池窗口缩略图仍存在 fallback 兜底，窗口级缩略图完整性需持续观察
4. 精选瞬间独立数据源已切分，仍需页面层最终验收
5. 候选池 stalled/failed 还需真实异常态截图最终验收
6. prompt JSON 不应走 shell 直拼
7. 需通过跨样本验证确认能力可迁移
8. 详见 `docs/TECH_DEBT.md`

---

## F. 4 月 22 日已完成修改（页面与流程）

### F.1 正式主链修复
- ✅ L2 三档审查接入 `process_v15_task()`（Pro 分层后自动调 `combined_review`）
- ✅ API key 延迟加载（`combined_review.py` 不再模块级绑定空值）
- ✅ L2 产物写入 `outputs/{task_id}/l2_clean_windows_full.json`
- ✅ L2 失败时 `pool_phase=failed`，成功时才 `pool_phase=completed`
- ✅ 补跑脚本 `scripts/task005_l2_backfill.py` 已废弃登记
- ✅ L3 prompt 文件头版本号修正为 v7.2.0

### F.2 候选池页面修复
- ✅ processing 时禁止 fallback 到旧 full_run（有 task_id 时 actual_l2_path=None）
- ✅ 预估时间平滑（保守初始估 + 移动平均 + 上调限速 + 超时文案切换）
- ✅ stalled / failed 失败闭环接入页面（5 分钟无更新→stalled，异常→failed）
- ✅ 窗口级缩略图生成（L2 完成后 ffmpeg 截取中间帧）
- ✅ 缩略图读取优先 `outputs/{task_id}/thumbnails/`
- ✅ tos_key 改为从 task material_status 读取真实路径（不再硬编码旧 task_013）
- ✅ 排序稳定（按 pool_level → source_file → raw_start_sec，不受区间修改影响）
- ✅ "已禁用" tab 新增，禁用/启用操作不整页刷新

### F.3 精选瞬间修复
- ✅ 数据源独立（highlights API 从 best_moment_candidates 构建）
- ✅ 轮询保留当前视图模式（不再强制切回 windows）
- ✅ highlights API 返回 pool_status（不再为 null）
- ✅ start_sec/end_sec 正确传递（修复 `|| 0` falsy 问题）

### F.4 裁剪弹窗修复
- ✅ 播放时间与裁剪区间彻底分离（自定义叠加条显示 clipStart / clipEnd）
- ✅ 拖蓝色左锚点只更新开始时间，拖红色右锚点只更新结束时间
- ✅ 区间保存持久化（delta 绝对值写入 overrides，刷新后一致）
- ✅ 保存/禁用后原地更新列表，不整页刷新
- ✅ AI 识别内容优先读 Flash 摘要，标签绿色
- ✅ 字号调大（标签 12px 加粗，正文 13px）

### F.5 当前已知未完全收口
- ⚠️ 裁剪弹窗区间保存后列表显示偶尔不一致（排序已修，待最终验收）
- ⚠️ 精选瞬间与候选池卡片 best_moment 匹配逻辑偏严（部分重叠未匹配）
- ⚠️ 跨样本验证尚未开始

## G. 4 月 22 日晚间追加修改

### G.1 正式一键生成链路
- ✅ 新建 `pipeline/generate_video.py`（一键生成核心逻辑）
- ✅ 新建 `/api/ui/generate` API（POST，异步后台执行）
- ✅ 正式链路：`build_l2_segments_text(task_id)` → L3 动态调用 → TTS → 字幕 → 渲染
- ✅ 不再走旧 fixed timeline / v9\_fixed\_render.py
- ✅ 人工 overrides 真实进入出片链（日志确认）
- ✅ ffmpeg/ffprobe 统一绝对路径（mainchain\_config.py）
- ✅ 阶段状态写回 `task.generate_stage`（tts/l3/download/clip/concat/render/done）
- ✅ 版本记录写入 `task.versions` 数组
- ✅ 视频文件服务 `/api/ui/video/{task_id}/{filename}`

### G.2 工作台前端闭环
- ✅ 生成按钮接正式链（`generateVideo()` → `POST /api/ui/generate`）
- ✅ 按钮文案动态切换（无版本→"生成视频"，有版本→"重新生成"）
- ✅ 生成进度 7 阶段实时显示
- ✅ 成片预览 + 下载 + 历史版本记录
- ✅ 音色切换（女声清和 / 男声奕辰）+ 预计时长联动
- ✅ 配置区文案收口 + 字幕面板精简
- ✅ 三栏折叠/展开 + localStorage 记忆
- ✅ 编辑层/渲染层分离（用户输入不被系统精修篡改）

### G.3 当前未收口的 4 个核心问题 ⚠️
1. **成片没有配音** — TTS 音频可能未正确混入最终视频
2. **成片保留原声** — 素材原声未静音或未按策略处理
3. **字幕卡点不对** — 字幕时间轴可能未按 TTS 时间戳对齐
4. **版本预览不稳定** — 预览/下载链路偶尔不回填

## G2. 2026-04-28 全天进度总结

2026-04-28 已完成：系统稳定性（v10.8 三刀修）、字幕 jieba 重构（v10.3/v10.4）、质量闸门全模式统一（v10.5-v10.6.2）、FINAL_VALIDATE 最终裁判层（v10.9）、slot 语义标签（v11 semantic_hint）、镜头理解层（v11.0 shot_understanding）。

## G3. 2026-04-29 全天进度总结

**v11.6.1_stable_baseline 已固化。** 当前稳定成片：`task_20260427_001_20260429_153049.mp4`（41.9s, tpad=0, 18 slot 全满）。

2026-04-29 已完成：
- **v11.0** shot_understanding P02/P20 误标修正
- **v11.2** semantic_selection_check + semantic_replan_once（语义校验+自动重选）
- **v11.3** L3 prompt v8.1（语义选片引导），首次 hard_fail 从 2→1
- **v11.4** 后验过度限制收口（短镜头 1.5→1.0s / 横幅上限 2→3 / why 退化停用），tpad 4.6s→0.3s
- **v11.5** 段落叙事审计（发现 L3 按单 slot 选片 + 无段落分组）
- **v11.5b** 场景域字段升级（+location_context/event_phase/audience_role/scene_group_id）
- **v11.6** L3 跨 slot 上下文增强（prompt v8.2 + narrative_continuity_report）
- **v11.6 回归修复** shortfall 计算修复 + FINAL_VALIDATE 收紧（tpad>0.8s + slot 缺失）+ 前端 stale 修复
- **v11.6.1** 补位横幅过滤弱化（含人物的不排除），slot_17 缺失修复，tpad=0

**当前稳定版本**：v11.6.4_stable_baseline
**L3 prompt**：v8.2
**shot_understanding**：v11.5b_scene_context
**验证通过任务**：task_20260426_001 / task_20260427_001 / task_20260429_001（三条 tpad=0，无尾部静帧）

## H. 当前下一步唯一动作

**→ v12.3 导演基线固化，进入观察期**

v11.7-v12.3 全系列已完成：
- P0 concat timeout ✅ / P1 缓存 ✅ / P2 L2并发5 ✅ / P3 分层并行 ✅
- 预排序停用 ✅ / 分层精简 ✅ / L3 输出精简 ✅
- 场景字段重构 ✅ / 场景结构对齐 ✅ / L3 场景参考注入 ✅
- 灰度上线 ✅ / 节奏抑制轻惩罚 ✅

**质量指标**：hard\_fail=0 / weak\_jump≤6 / bad\_jump=0 / 后验修正=0
**总耗时**：81 分钟 → 20-25 分钟

**当前状态**：观察期。v12.6 系统冻结规则有效 + v13.0 L3 导演层基线已确认。

系统基线版本：v12.6\_freeze\_fix\_baseline
L3 导演层基线版本：**v13.0-pre3.1**（三目标平衡策略）
配置：USE\_SCENE\_STRUCT\_MODE=gray / 节奏抑制=v12.3 / PRO\_MAX\_RETRY=0 / PRO\_CALL\_TIMEOUT=300s / v12.6 短窗口替换

**当前系统状态：🟢 生产就绪（v12.6 系统 + v13.0 L3 导演，4 样本验证通过，平均综合 4.13/5）**

v13.0 L3 导演层文档：`docs/V13_DIRECTOR_BASELINE_PRE3_1.md`

下一阶段可考虑：新 narration 任务积累验证 / 补位稳定性优化 / scene_struct 全量启用 / 前端优化

---

## I. 关键路径速查

| 用途 | 路径 |
|------|------|
| render 入口 | `scripts/v9_fixed_render.py` |
| L2 prompt | `prompts/video_news/l2_three_tier_review_prompt_v1.txt` |
| L3 prompt | `prompts/video_news/l3_director_prompt_v7.txt` |
| L2 后处理 | `pipeline/combined_review.py` |
| 主链配置 | `pipeline/mainchain_config.py` |
| 字幕生成 | `pipeline/tts_provider.py` |
| 字幕样式 | `pipeline/render_preflight_checks.py` |
| 人工覆盖 | `pipeline/pool_overrides.py` |
| 主片池 | `outputs/full_run/l2_clean_windows_full.json` |
| 工作台前端 | `webui/dist/task-workbench.html` |
| 工作台后端 | `app/main.py` |
| 任务处理 | `pipeline/tasks.py` |
| EXE GUI | `v15-tasklink/main_gui.py` |
| GitHub workflow | `v15-tasklink/.github/workflows/build-v15-gui.yml` |
| 阶段验收样片 | `outputs/v12_final/v12_final_20260421_114929.mp4` |
| 验收报告 | `docs/V12_STAGE_ACCEPTANCE.md` |
| 跨样本计划 | `docs/CROSS_SAMPLE_VALIDATION_PLAN.md` |
| 主链文档 | `docs/NEWS_VIDEO_OFFICIAL_CHAIN.md` |
| 规则文档 | `docs/NEWS_VIDEO_RULES_AND_PROMPTS.md` |
| 构建文档 | `docs/NEWS_VIDEO_BUILD_AND_RELEASE.md` |
| 状态机文档 | `docs/NEWS_VIDEO_WORKBENCH_STATUS_MACHINE.md` |
| 阶段状态 | `docs/NEWS_VIDEO_STAGE_STATUS_20260421.md` |
| 文件索引 | `docs/NEWS_VIDEO_FILE_INDEX.md` |
| 技术债 | `docs/TECH_DEBT.md` |
| prompt 变更 | `prompts/CHANGELOG.md` |
| 本文件 | `00_READ_THIS_FIRST.md` |
| 机器状态 | `project_runtime_state.json` |

---

## J. 禁止再走的旧路径

| 类别 | 禁止项 |
|------|--------|
| deprecated render | `v7_diverse_render.py` / `v8_*_render.py` 系列 |
| 旧 L2 prompt | `l2_video_review_prompt_v2.txt` / `l2_l28_combined_prompt_v1.txt` |
| 旧 L3 prompt | `l3_thinking_prompt_v2.txt` / v3-v6 系列 |
| 旧字幕链 | 字符比例估算方式 |
| 旧 L2 结果 | 不允许沿用 v1.0/v1.1 时代的 L2 结果混跑新主链 |
| 临时验证链 | 不允许在正式主链外拼独立链冒充主链 |
| **候选池默认 completed** | 不允许 pool_status.state 硬编码为 completed |
| **精选瞬间读 clean_windows** | 精选瞬间必须从 best_moment_candidates 读取 |
| **无失败闭环 processing** | 候选池 processing 必须有 stalled/failed 检测 |
| **full_run 混入新 task** | 新 task 优先读 outputs/{task_id}/，不直接读 full_run |
| **补跑脚本造 L2 产物** | `scripts/task005_l2_backfill.py` 已废弃，禁止执行（2026-04-22） |
| **双字词 fallback 字幕分词** | `_tokenize_by_phrases` 中禁止恢复 `text[pos:pos+2]` 双字词逻辑（v10.4 废弃） |
| **绕过字幕自检** | 任何字幕路径必须调用 `validate_subtitles_no_split()` 才能烧录（v10.4 固化） |
| **词库补丁替代机制修复** | 禁止只加 PHRASES_NO_SPLIT 词条而不修分词机制（v10.4 规定） |

## K. 4 月 23 日上午工作台小屏适配 + 备份

### K.1 小屏/平板基础响应式适配
- ✅ viewport 允许用户缩放（0.5x-3x）
- ✅ body overflow:hidden 修复，小屏可正常滚动
- ✅ 三栏布局响应式：≥1200px 三栏 / 768-1200px 双栏+通栏 / <768px 纯单栏
- ✅ 触摸滚动优化 + 裁剪弹窗触摸事件修复

### K.2 左侧阶段栏折叠
- ✅ 小屏下折叠为顶部摘要条（默认收起，点击展开）
- ✅ 摘要文案体现完整工作流：阶段名 + 审片状态 + 候选池 + 成片状态
- ✅ 数据漏斗概览随阶段栏一起收纳

### K.3 手机屏信息折叠模式（≤480px）
- ✅ 顶部折叠：任务名 + task_id + 上次保存时间（暂存按钮不折叠）
- ✅ 素材区下拉筛选器（全部/已分析/待分析）
- ✅ 候选池 tab 下拉选择器（5 项）
- ✅ 下拉菜单 z-index 200 + panel overflow:visible（不再被遮挡）

### K.4 标题区与样式统一
- ✅ 顶部品牌标题"元泉·智影工场"（17px/700 主标题样式）
- ✅ 三主区标题行结构统一：左标题 + 右筛选/切换 + 胶囊折叠按钮
- ✅ 折叠按钮重做：胶囊式 + 文字"收起/展开" + 箭头旋转动画
- ✅ 小屏横向溢出修复：html overflow-x:hidden + min-width:0 + padding 收紧

### K.5 安全快照备份
- ✅ 备份目录：`backups/workbench_snapshot_20260423_0900/`
- ✅ 备份范围：前端 + 后端 + pipeline + prompts + config + 状态档案 + docs + logs（28 文件）
- ✅ 备份清单：`backups/workbench_snapshot_20260423_0900/BACKUP_MANIFEST.md`
- ✅ 恢复方法已写入清单

### K.6 当前页面状态
**接近可用 — 可进入成片质量优化阶段**

## L. 4 月 23 日下午主链固化审计

### L.1 固化审计结论
- ✅ 全部 20 个主链模块已达 D 级（代码 + 规则文件 + 真实验收）
- ✅ 无任何模块停留在 A/B/C 级
- ✅ 脱离当前对话后系统可独立执行完整主链

### L.2 新增正式文件
- `docs/VIDEO_MAINCHAIN_SOLIDIFICATION_AUDIT_20260423.md` — 固化审计总表（20 模块）
- `docs/VIDEO_MAINCHAIN_PRECHECKLIST.md` — 下一次开工前检查单
- `docs/SUBTITLE_RULES.md` — 字幕正式规则（样式/分句/标点/位置/时间轴）

### L.3 当前唯一正式 task 主链入口
`POST /api/ui/generate` → `pipeline/generate_video.py` → `generate_video(task_id)`

### L.4 当前下一步唯一主线
**画面调度质量优化 + 跨样本验证**

### L.5 运维要点
- 修改 Python 代码后**必须重启 Flask**
- 音色读取链：`tasks/configs/{task_id}.json` → generate_video.py → TTS
- 字幕完整性兜底：若切分丢字自动回退到 TTS 原始分段

## M. 4 月 23 日傍晚稳定快照备份

### M.1 备份信息
- ✅ 备份目录：`backups/video_mainchain_snapshot_20260423_1805/`
- ✅ 备份范围：32 个文件（前端+后端+pipeline+prompts+config+状态+docs+logs）
- ✅ MD5 校验通过

### M.2 当前系统状态
**阶段性可用 — 稳定快照**

今日已完成：
- 工作台小屏适配（响应式三档 + 手机折叠模式）
- 生成主链核心修复（配音/原声/音色/字幕/时长/预览/下载）
- 字幕链收口（安全切分 + 标点归属 + 弱标点去除 + 完整性兜底）
- 字幕样式固定（26px 黑体 + 1px 描边 + 1px 阴影）
- 主链固化审计（20 模块全部 D 级）

### M.3 当前下一步
画面调度质量优化 + 跨样本验证

## N. 工作台阶段状态修复 (2026-04-23 18:24)

### N.1 问题
样片已生成 29 个版本，但左侧仍显示"样片渲染输出进行中"

### N.2 根因
- `getSteps()` 基于 `task.status` 字符串映射，无法区分"正在生成"和"已生成完成"
- `task.progress` 数字映射逻辑错误

### N.3 修复
- 新增 `computePipelinePhase()` 函数，基于真实数据推导阶段
- 重写 `getSteps()` 和 `renderSteps()`
- 修复数据漏斗标签："进池候选" → "候选片段"
- 修复自动刷新条件，增加 pool_status 检查

### N.4 阶段口径 (5 阶段)
1. 本地素材上传 (20%)
2. 大模型审片与打标 (40%)
3. 候选片段提炼 (60%)
4. 视音频参数配置 (80%)
5. 样片渲染输出 (100%)

### N.5 验证
- ✅ task_20260422_002: 29 版本，显示"样片渲染输出 已完成 (100%)"
- ✅ 左侧 5 阶段全部显示绿色对勾
- ✅ 数据漏斗：素材总数 19 | 已分析 19 | 待分析 0 | 候选片段 0

### N.6 文档
- docs/WORKBENCH_STAGE_FIX_20260423.md

## O. 纯音乐混剪模式实现 (2026-04-23 20:24)

### O.1 模式总规则
- **字段**: `edit_mode` = `narration` / `music_only`
- **二选一**: 前端切换 + 后端保存 + L3 输入 + 生成主链全链路贯通

### O.2 新闻播报模式调整
- 口播稿字数建议: 350 字 → 300 字
- 主链逻辑保持不变

### O.3 纯音乐混剪模式新增
- 视频时长选择: 30 秒 / 45 秒 (默认 30)
- 背景音乐配置: TOS Music 目录 3 首 + 本地上传 (mp3/wav/m4a, ≤20MB)
- 分镜要求文本框
- 纯音乐模式专属字幕控制 (含动态字幕文本)

### O.4 字幕配置分离
- `news_subtitle` (新闻播报模式)
- `music_subtitle` (纯音乐混剪模式)
- 各自独立保存，切换模式不互相覆盖

### O.5 文本框自动保存
- 支持: 新闻稿、分镜要求、纯音乐字幕文本
- 触发: blur 事件 (点击外部)
- 持久化: tasks/configs/{task_id}.json

### O.6 生成主链分支
- 新闻播报: TTS → L3 → 裁切 → 拼接 → 字幕 → 渲染
- 纯音乐: 跳过 TTS → L3 (传入分镜/BGM/字幕) → 裁切 → 拼接 → 下载 BGM → 渲染

### O.7 验证
- ✅ 配置保存/读取正常
- ✅ BGM 上传格式校验正常
- ⏳ 完整生成流程待人工验收

### O.8 文档
- docs/MUSIC_ONLY_MODE_IMPLEMENTATION_20260423.md

## P. 纯音乐混剪模式补齐 (2026-04-23 20:50)

### P.1 补齐内容
- ✅ 20 秒时长选项 (位于 30 秒之前)
- ✅ BGM 播放/暂停按钮 (带状态切换)
- ✅ 字幕控制关闭时灰态不可编辑 (opacity:0.4, pointer-events:none)

### P.2 修改文件
- webui/dist/task-workbench.html
  - 新增 20 秒 radio 选项 (~644 行)
  - 新增 BGM 播放按钮 HTML (~659 行)
  - 新增 toggleBgmPlay() 函数 (~2445 行)
  - 重写 toggleMusicSubConfig() (~2438 行)

### P.3 生成验证
- ✅ 配置正确保存 (edit_mode=music_only, target_duration=30, bgm_tos_key=政务活动背景音乐.mp3)
- ✅ 生成流程启动 (/api/ui/generate)
- ⏳ L3 执行中 (纯音乐模式跳过 TTS，直接 L3)

### P.4 待验收
- L3 完成后验证 BGM 下载与合成
- 验证成片时长是否符合 target_duration
- 验证分镜要求是否影响 L3 选镜


## Q. 纯音乐混剪模式 L3 Prompt 分离 (2026-04-23 21:15)

### Q.1 决策
走方案 A：新建独立 prompt，不复用新闻播报 v7.2

### Q.2 新建文件
- `prompts/video_news/l3_music_montage_prompt_v1.txt`（纯音乐混剪专用导演 prompt）
- `docs/MUSIC_MONTAGE_RULES.md`（纯音乐模式正式导演规则）

### Q.3 代码修改
- `pipeline/generate_video.py`：新增 `L3_MUSIC_PROMPT_FILE`，L3 调用按 `edit_mode` 分支选择 prompt

### Q.4 UI 修改
- 纯音乐模式说明文案："模式说明" → "提示"，"氛围向" → "氛围感"

### Q.5 真实验证
- ✅ 成片：`task_20260422_002_20260423_211254.mp4`
- ✅ 时长：30.04s（目标 30s，误差 +0.04s）
- ✅ 镜头：11 个，11 个不同素材，平均 2.7s/镜头
- ✅ BGM：政务活动背景音乐.mp3 已合入成片
- ✅ 风格：短镜头快切，氛围感导向，明显区别于新闻播报
- ✅ 不再超时（L3 调用正常返回）

## R. 纯音乐混剪节奏重构 + BGM 淡出 (2026-04-23 21:55)

### R.1 节奏重构
- prompt v1.1 收紧：30s 模式目标 13-15 镜头，平均 2.0-2.4s
- 禁止连续 2 个以上 ≥3.0s 镜头，结尾单镜头不超 3.0s
- 禁止 3×3s 平铺、4s+ 收束拖尾

### R.2 对比（30s 样片）
| 指标 | v1.0 | v1.1 收紧后 |
|------|------|-----------|
| 镜头数 | 11 | 13 |
| 平均时长 | 2.73s | 2.31s |
| ≥3s 镜头 | 6 | 1 |
| 结尾 | 3+4=7s | 2.5+3=5.5s |

### R.3 BGM 淡出
- 方式：预处理 BGM 音频，ffmpeg afade=t=out
- 淡出时长：3.0 秒
- 适用：20/30/45 秒模式统一
- 文件：pipeline/generate_video.py

### R.4 最新验证
- 成片：task_20260422_002_20260423_215513.mp4 (30.08s)
- 13 镜头，12 素材，平均 2.31s，1 个 ≥3s
- BGM 淡出已写入主链（预处理方式）

## S. 女声语速 + 纯音乐字幕分离 + 黄字描边 (2026-04-23 22:31)

### S.1 女声语速
- S_x249qIGO1: 1.0 → 1.15（提速 15%，实测 238字≈50.4s）
- S_BY29qIGO1: 0.95（不动）

### S.2 纯音乐字幕规则分离
- 新闻播报：短句碎切，紧跟 TTS timing
- 纯音乐：按句出现（句号/感叹号/问号分句，逗号不切），句间 0.3s 间隔

### S.3 黄字描边样式
- PrimaryColour: &H0048CCFF (偏橘黄)
- OutlineColour: &H00003366 (深棕)
- Outline: 2px, Shadow: 0
- FontSize: 24 (略小于新闻播报 26)

### S.4 验证
- ✅ 新闻播报: task_20260422_002_20260423_221851.mp4 (55.44s)
- ✅ 纯音乐: task_20260422_002_20260423_223102.mp4 (31.08s)
- ✅ 纯音乐字幕: 3 句按句出现（vs 新闻播报 33 段碎切）
- ✅ 黄字描边样式: yellow_outline 已接入

## T. 女声再提速 + 纯音乐字幕单行优先 + 位置上移 (2026-04-23 23:02)

### T.1 女声语速
- 1.05 → **1.15**（同篇稿 54.4s → 50.4s → 实际成片 51.96s）
- 男声 0.95 = 44.7s，女声 1.15 仍比男声长约 6s（女声音色特性）

### T.2 纯音乐字幕
- 单行优先：每条 ≤16 字，超过在逗号/顿号处切分
- 无逗号的长句在中间硬切
- 句间 0.2s 空隙
- 8 条字幕全部 ≤16 字，0 条超限

### T.3 纯音乐字幕位置
- MarginV: 30 → **50**（上移 20px，离底部更远）
- 仅影响纯音乐模式，新闻播报模式不变

### T.4 验证
- 新闻播报: task_20260422_002_20260423_225820.mp4 (51.96s, 女声 1.15)
- 纯音乐: task_20260422_002_20260423_230155.mp4 (30.04s, 黄字描边)

## U. 女声再提速 + 字幕时长联动 + 预计时长修正 (2026-04-23 23:31)

### U.1 女声语速
- 1.05 → **1.15**（同篇稿 54.4s → 50.4s，成片 51.96s）

### U.2 纯音乐字幕时长联动
- 20 秒：MAX_CHARS=12（最严格），建议 ≤90 字
- 30 秒：MAX_CHARS=16，建议 ≤120 字
- 45 秒：MAX_CHARS=18，建议 ≤160 字
- 字数建议随时长切换即时更新

### U.3 新闻播报预计视频时长
- 旧值：女声 6.3 字/秒，男声 6.0 字/秒（严重失真）
- 新值：女声 **4.72** 字/秒，男声 **5.33** 字/秒（基于实测数据）
- 文案："预计XX秒" → "预计视频时长 XX 秒"

### U.4 验证
- 20s 纯音乐: task_20260422_002_20260423_233107.mp4 (20.04s)
- 字幕: 5 条，全部 ≤12 字，最长 11 字

## V. 收工存档与备份 (2026-04-23 23:45)

### V.1 今日完整改动
详见 `docs/VIDEO_WORKBENCH_AND_MAINCHAIN_STATUS_20260423.md`

### V.2 备份
- `backups/video_workbench_mainchain_snapshot_20260423_2345/`（29 个文件，MD5 校验通过）

### V.3 当前阶段
**双模式收口快照** — 新闻播报 + 纯音乐混剪均已阶段性可用

### V.4 下一步主线
画面调度质量优化 + 跨样本验证

### V.5 明天开工先看
1. 00_READ_THIS_FIRST.md
2. project_runtime_state.json
3. docs/VIDEO_WORKBENCH_AND_MAINCHAIN_STATUS_20260423.md
4. docs/VIDEO_MAINCHAIN_PRECHECKLIST.md
5. docs/MUSIC_MONTAGE_RULES.md

## v7.3 正式固化（2026-04-24）

### 三项核心升级

1. **L2 定向二次稳定性筛查**（pipeline/tasks.py）
   - 对 strong_safe 段做 ffmpeg pblack 二次校验
   - pblack < 80% → 降为 weak_safe
   - 触发位置：L2 逐条审查完成后、写入产物前

2. **L3 候选池层级约束**（pipeline/pool_overrides.py）
   - disabled → 完全不可见
   - backup → 🟡仅补位可用
   - primary → 🟢优先可用
   - build_l2_segments_text() 和 build_l3_video_inputs() 同时受约束

3. **L3 视频精看**（pipeline/generate_video.py）
   - L3 不再只读文字，改为看"完整合法候选段视频 + 文字 prompt"
   - 候选段从 clean_windows 裁出，保护人工确认的完整边界
   - best_moment 仅作段内提示（文字标注），不切坏候选段
   - 豆包 Pro 单次最多 10 条视频，超出时优先 primary

### 旧路径废弃
- L3 纯文字模式：降级为兜底（deprecated），正式入口走视频精看
- best_moment 小片段作为主输入：不再采用，改为完整候选段

### 正式入口
- generate_video() → build_l3_video_inputs() + _call_l3_director(video_clips=)

## L2 耗时专项优化（2026-04-24）

### L2 三档审查并发化
- **旧方式**：串行逐条，19 素材 ~26 分钟
- **新方式**：3 路 `ThreadPoolExecutor` 并发，19 素材 ~9 分钟
- **加速比**：2.8-2.9x
- **总流程**：19 素材从 ~35 分钟降到 ~16 分钟
- **并发度**：3 路（火山引擎视频理解 API 安全并发度）
- **文件**：pipeline/tasks.py
- **进度更新**：线程安全，实时写回 task JSON
- **API 限流**：3 路已验证稳定，如需更高并发需联系火山引擎确认 RPM 限制

## 主链优化收口（2026-04-24 第二轮）

### 1. 补位逻辑统一输入源
- 补位不再直接读原始 l2_clean_windows_full.json
- 统一走 load_pool_data() + apply_overrides_to_pool() + pool_level 约束
- disabled 不可补位，primary 优先补位
- 文件: pipeline/generate_video.py

### 2. 精彩瞬间退出前端
- 前端不再显示"精选瞬间" tab、下钻链接、下拉选项
- 后端保留 best_moment_candidates 作内部辅助字段
- 文件: webui/dist/task-workbench.html

### 3. L2 冗余输出精简
- 删除 7 个冗余字段: recommended_segments, fallback_segments, formality_summary, technical_summary, boundary_summary, selection_summary, newsworthiness
- 精简 best_moment 子字段: 删除 candidate_id, duration, expression_quality, action_completion, pose_stability
- L2 prompt v1.2.0 → v1.3.0
- 预估每条 L2 减少 ~30% 输出 token，提速 10-20%
- 文件: prompts/video_news/l2_three_tier_review_prompt_v1.txt + pipeline/combined_review.py

### 4. 预排序阈值 20 → 30
- ≤30 条素材跳过预排序（~40s）
- >30 条才启用预排序
- 文件: pipeline/tasks.py

## ffmpeg 窗口级后处理正式固化（2026-04-24）

### 模块
- 文件: pipeline/ffmpeg_stability.py
- 接入点: pipeline/tasks.py → process_v15_task() → L2 完成后调用 process_l2_windows()
- 旧 task 兼容: pipeline/generate_video.py → generate_video() 入口自动检测并补跑

### 处理动作
- keep: 整段保留
- trim_head: 前 1 秒不稳 → 起点后移到 1.5s
- trim_tail: 收尾脏边界 → 终点前移 0.5s
- downgrade: 整段不稳 → 降为 weak_safe

### 输出字段
写回 clean_windows 每个窗口:
ffmpeg_stability, ffmpeg_pblack, ffmpeg_head_pblack, ffmpeg_tail_pblack,
ffmpeg_action, suggested_start_sec, suggested_end_sec, head_unstable, tail_dirty

### 后续读取
- build_l2_segments_text(): 传递给 L3 文字 prompt
- build_l3_video_inputs(): 按修正后起止点裁片
- 补位逻辑: 统一走 load_pool_data + apply_overrides（包含 ffmpeg 结果）

## 项目状态存档（2026-04-25 01:30）

### 当前主问题（P0）
**新闻播报模式组片质量偏差**
- 举横幅镜头被重复使用且停留过长
- 整体节奏偏拖沓
- 运镜素材应只取稳定可用段，不要把试运镜/找位/回摆全过程放进成片

### 后续推进主线
不要分散修小点，优先围绕"新闻播报模式组片质量"持续优化：
- 镜头选择是否更贴题
- 横幅/合影类镜头是否减少重复
- 节奏是否明显更紧凑
- 运镜素材是否只取稳定可用段

---

## 新闻播报模式当前状态（2026-04-25 v7.4 固化）

### 当前主问题

1. **L3 视频输入存在但理解深度不足**
   - 视频片段确实上传并送给了 L3
   - 但 L3 决策主要依赖场景标签和文字描述
   - 视频内容理解深度不足

2. **新闻稿驱动层已从弱提示升级为硬约束**
   - ✅ 新闻稿结构化解析（opening_theme / main_body / highlight / closing）
   - ✅ 主信息镜头优先于氛围镜头
   - ✅ 段落 - 镜头匹配（opening 段优先落在片头 1-2 个镜头）
   - ✅ 后验自动修正（片头点题、片尾收束、横幅重复控制、主体段覆盖、亮点段优先）

3. **字幕链与新闻稿/TTS 存在脱节风险**
   - 新闻稿切句可能不准确
   - TTS 时间戳与字幕时间戳可能不同步
   - 需进一步验证具体脱节情况

### 推进优先级

| 优先级 | 任务 | 状态 |
|--------|------|------|
| 🔴 1 | 新闻播报模式：新闻稿驱动约束 | ✅ 已完成 v7.4 固化 |
| 🟠 2 | 纯音乐模式：视频输入稳定性修复 | 待执行 |
| 🟡 3 | 新闻播报模式：L3 读片能力增强 | 待执行 |

### 规则文件位置

- 新闻播报模式规则：`docs/NEWS_SCRIPT_DIRECTOR_RULES.md`
- 基础规则层：`docs/BASE_DIRECTOR_RULES.md`
- 项目状态：`project_runtime_state.json`

### 未收口点

1. L3 视频输入稳定性（有时无视频，退化为文本组片）
2. L3 真正看视频能力（视频理解深度不足）
3. 字幕链与新闻稿/TTS 对齐（需进一步验证）

---

## 当前 P0/P1 问题清单（2026-04-26 00:22 存档）

### P0：新闻播报模式生成连续失败
- 状态：部分修复，需验证
- 连续 3 次失败，已修复 pool_overrides.py/generate_video.py/tasks.py/前端
- 需要真实新建 task 完整验证
- 详见：`docs/VIDEO_PROJECT_CURRENT_ISSUES.md`

### P1：纯音乐混剪模式后半部分/结尾质量不足
- 状态：已识别，部分规则已加，需验证
- 结尾弱信息镜头堆叠、视觉母题重复、可补充镜头未调入
- 详见：`docs/VIDEO_PROJECT_CURRENT_ISSUES.md`

### 推进原则
1. 不要再零散补丁式修复
2. 先稳定新闻播报模式生成成功率（P0）
3. 再处理纯音乐尾部质量（P1）
4. 所有修复必须先确认真实入口、文件级修改、真实验证


## 2026-04-26 全天工作存档

### 当前阶段
**时间坐标+时长断点联合审计（最高优先级）**

### 当前主链
- global_reel_l3 = True（全局候选长片读片已接入生产）
- prompt = 视频优先导演模式（从零版，~80行）
- L3 输入 = candidate_reel_small.mp4
- TTS 在 L3 前生成，时长已传给 L3

### 当前 P0 问题
**时间坐标与最终渲染链路不可信**
- L3 选片越界（end 超出素材时长）→ 丢 3.88s
- concat 跳过极短 clip → 丢 2.50s
- tpad 静帧补 5.26s
- 趣味互动卡点偏差 0.6s

### 明天开工先做
1. 时间坐标+时长断点修复
2. 不要先修规则/语义/导演
3. 先读：
   - 00_READ_THIS_FIRST.md
   - project_runtime_state.json
   - docs/TIME_COORDINATE_AUDIT_PENDING.md
   - docs/VIDEO_PROJECT_CURRENT_ISSUES.md
   - docs/L3_GLOBAL_REEL_DEBUG_LOG.md

### 暂停方向
- expression_intent / dominant_intent 规则
- 四类交替规则
- POC 冒充生产

### 保留原则
- 规则要少，只保留底线
- 让大模型真实看视频
- 以最终成片和 final_render_timeline 为准
