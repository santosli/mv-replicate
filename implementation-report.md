# 可靠性改进实施报告

## 起止状态

- 起点：`4ad21d7`（main，工作区干净，与 origin/main 一致）。
- 工作分支：`reliability`（本地），按阶段提交：`8ad88d0`（阶段一）、`1322fee`（阶段二）、`e4ad3a9`（阶段三）、`4e2a5c4`（阶段四），之后一个提交是真实素材回放后的修正和本报告；结束提交见 `git log -1 reliability`。
- 结束时工作区干净；没有 push、PR 或合并。
- 环境：macOS 27.0.1（arm64），Python 3.12.13，FFmpeg 8.1，Pillow 13.0.0，librosa 1.0.1，numpy 2.5.5，soundfile 0.14.0。没有在其他系统或 FFmpeg 版本上运行。

## 共用约定

时间、帧数量化、取帧规则、AVOID 语义、逐字时间来源、报告和版本约定写在 `references/conventions.md`，实现集中在 `scripts/mvr_common.py`，校验器、生成器、渲染器、抽帧和 QA 共用。

取帧模型（输出第 k 帧显示源时间 `from + k × speed / fps` 时屏幕上的那一帧，时间先取整到流的 time base 刻度）用渲染图 `setpts=trunc((PTS−S)/speed)` + `fps=…:round=up:start_time=0` 实现。开发时对 4 种素材（B 帧短 GOP 60 fps、30000/1001、起始 PTS 0.37 s、8 帧 VFR）× 7 个起点 × 3 个输出帧率 × 3 个速度 × 正放/倒放/定格做了 756 组逐帧比对，模型与 FFmpeg 实际输出全部一致；回归集里保留了其中 54 组（T06）。

## 问题、修复位置与外部行为变化

### 阶段一：目录安全、前置校验、失败协议

| 问题 | 修复位置 | 行为变化 / 兼容 |
|---|---|---|
| `work_dir='tmp/..'` 递归删除项目；清理发生在校验前 | `render_edl.py` `main`，`mvr_common.run_dir` | `work_dir` 变为临时父目录，每次新建唯一子目录，只删这个子目录；校验在创建任何东西之前完成。迁移说明见 README |
| 输出可覆盖输入 | `validate_edl.py` 路径检查 | 输出与歌、素材、叠层、字体、EDL（按解析后的真实路径和同一文件判断）冲突时拒绝 |
| 失败可能留下半成品、覆盖旧成品 | `render_edl.py`、`mvr_common.publish` / `output_lock` | 成片先写隐藏候选文件，QA 通过才发布；已有输出默认拒绝，`--replace` 只在新片通过后原子替换；同一目标用 flock 互斥（进程退出自动释放） |
| 没有前置校验 | 新增 `validate_edl.py`（渲染入口自动调用） | 字段、有限数、尺寸偶数、段落连续、帧数、源素材长度、叠层、字体、AVOID、路径冲突；错误定位到 `segments[i].字段` / `overlays[j].字段` |
| 没有环境检查 | 新增 `doctor.py` | 按任务检查依赖、编码器、滤镜、字体、whisper 和模型；不需要的项写 skipped / unavailable |
| 没有最终媒体 QA | 新增 `qa_render.py`（渲染入口自动调用） | 帧数 = Q(时长)、尺寸、帧率、音频解码长度、包连续性、最终 AAC 响度、PCM 与成片偏移；每项 pass / fail / skipped |
| 段落帧数按每段舍入累加，会漂移 | `mvr_common.Q`、`seg_frame_count` | 帧数 = `Q(at+dur) − Q(at)`，整片恒为 `Q(duration)` |

### 阶段二：抽帧、源帧选择、逐字时间

| 问题 | 修复位置 | 行为变化 / 兼容 |
|---|---|---|
| `-ss` 四位小数在 60 / 30000/1001 fps 下取错帧；按名义帧率和包数换算，VFR 漏帧 | `frame_sheet.py` 重写 | `--frames` 按真实解码帧号，`--at` / `--every` 按真实 PTS（PTS ≤ t 的最后一帧），`--dense` 的 step 是源帧步长，`--ends` 取真实首中末帧；标签和 `--json` 都是实际帧号和 PTS |
| 抽帧失败也返回 0；先装下全部原图再分页 | 同上 | 在 FFmpeg 里裁剪和缩放；按页提取、`--workers` 有界并发；任何请求失败默认非零且不写任何页（`--lenient` 显式宽松）；页面先写临时名，全部成功再发布 |
| AVOID 有 20 ms 放行；只查主素材；别名可绕过 | `mvr_common.check_avoid` / `segment_sources`，EDL 新字段 `avoid` | 按成片实际显示的每个源帧检查，含 `wipe_in` 第二路（保守）；`[a,b)` 内任何时刻在屏幕上的帧都算禁用；按设备号 + inode 识别同一文件；EDL 没有 `avoid` 时明确警告"未检查" |
| freeze 按输出时长当源区间；倒放带进多读的 0.1 秒；源不够长时 `max(start,0)` 凑长度 | `mvr_common.select_chain`、`render_edl.render_segment` | freeze 只检查并保持那一帧；倒放精确倒序所选帧；源不够长时校验失败 |
| DTW 缺失时静默退回 offsets；插值可产生倒序；英文词按字符/词单位错配 | `lyric_chars.py` 重写 | 见 `references/conventions.md`「逐字时间」；新增 `--overrides`（绑定歌词文件哈希，歌词变了判为过期）、`--override-template`、`--drop-conflicting-anchors`、`--empty-lines`；新增 `compare_lyric_timing.py` |

### 阶段三：效果、分析量纲、音频

| 问题 | 修复位置 | 行为变化 / 兼容 |
|---|---|---|
| `--yield` 截断所有含该文字的句子 | `lyric_layer.yield_to_title` | 只处理唱到时间离 T 最近的那一次；文本、字、`end`、`first_char_t` 同步更新；歧义时报候选，可用 `TEXT@T#k`；整句让位后正常跳过 |
| freeze 在部分 FFmpeg 版本只出 1 帧 | `select_chain`（fps 在 tpad 之前） | 按帧数和帧内容验收（T14） |
| 8/10 bit、limited/full range 的统计不可比；HDR 直接套 SDR 阈值；无音轨崩溃 | `analyze_ref.py` | 先在 FFmpeg 里转成 8 bit limited range 再统计；HDR 颜色分析写 unsupported；无音轨时音频写 unavailable |
| 固定 4 拍；无拍时 NaN/崩溃；相位写成真实强拍 | `analyze_song.py` | `--beats-per-bar`、`--bar-phase`、`--beats`；无拍时失败并提示 `--beats`；输出 `bar_phase_method` 和"这是假设"的说明 |
| alimiter 未开延迟补偿，PCM 晚约 5 ms | `render_edl.render_audio` | 4 倍过采样 + `latency=true` |
| 响度只看编码前 | `render_edl.audio_and_mux`、`qa_render.py` | 按最终 AAC 解码测量，最多修正 3 次；不达标默认失败、不发布、保留报告；`--allow-loudness-miss` 显式放行并记录偏差 |
| 叠层不在帧格上、负预滚整体平移 | `render_edl.overlay_span` | 叠层从 `Q(at)` 帧开始；负 `at` 保留动画时钟、裁掉 0 秒前部分并警告 |
| 叠层逐帧写 PNG | `mvr_common.encode_rgba`（render_edl、lyric_layer） | 直接把 RGBA 帧流式送给编码器；`wipe_in` 的遮罩仍是 PNG 序列（后续项） |

### 阶段四：版本和 skill 工作流

| 问题 | 修复位置 | 行为变化 / 兼容 |
|---|---|---|
| 版本靠改脚本文件名，产物会互相覆盖 | `examples/make_edl.py` 重写 | `--version` 决定 `versions/<版本>/` 下的 EDL、歌词图层、成片、QA 报告、联系表、manifest；已有成片的版本拒绝再写；`latest-passing.json` 只在 QA 通过后更新；manifest 的 `review` 记录人工认可，与机器 QA 分开 |
| 字卡退场按"最后一字 + 0.6 秒后的下一拍"猜，会压到下一刀 | `make_edl.title_overlay` | 按实际下一刀退场，太近时报冲突 |
| `--no-render` 可写出非法 EDL | `make_edl.main` | 先跑完整校验，不通过就什么都不写 |
| 计划写死在脚本里 | `--project`（`examples/project.example.json`）、`--preset`（`presets/default.json`） | 旧用法仍可运行（默认版本 `v1`，计划用脚本内默认值） |
| 通用规则、偏好、项目状态混在 SKILL.md | `SKILL.md` 重写，`references/house-preferences.md`、`references/case-study.md`、`references/conventions.md` | SKILL.md 只留触发范围、输入、必需规则、阶段流程、失败处理和交付标准；偏好标为预设 |
| 没有风格规则与验收的映射 | `examples/style-spec.example.json` | 每条规则有 id、参考区间、音乐功能、参数、目标段落、证据、状态和原因 |

## 测试

命令：

```bash
python tests/run_tests.py -v
```

结果：29 个测试（覆盖 T01–T27，T03 和 T06 各两个），全部通过，0 失败，0 跳过，用时约 40 秒。素材全部运行时合成，破坏性路径测试都在测试自建的 `临时目录/x/y/proj` 里进行。

关键证据：

- **危险路径**（T01、T02）：`work_dir='tmp/..'` 和指向别处的符号链接 `work_dir` 下，项目内全部文件哈希不变，没有遗留运行目录；输出指向 `clips/a.mp4`、经符号链接目录指向输入、指向 EDL 都被拒绝且输入不变；同一目标并发时第二个运行被拒绝；用 `--replace` 跑一个必然失败的渲染后，已发布成片哈希不变、没有遗留候选文件。
- **精确帧内容**（T05–T07、T09、T13、T14）：读的是拼图格子里的像素颜色（每帧颜色编码了自己的编号），不是标签。60 fps 和 30000/1001 fps 的第 1、4、7 帧正确；VFR 查 1.5 秒得到 PTS 1.4 的帧；倒放 10–19 得到 19…10；定格输出 10 帧且都是目标帧；抽帧时内存里最多 16 帧（2 个 worker × 每页 8 格）。
- **AVOID 次输入**（T10–T12）：`..` 路径、符号链接、`wipe_in` 第二路都被拦截；相接区间不误报；不对齐边界 `[0.95, …)` 拦住 PTS 0.9 的那一帧；60 fps 下第 58 帧（跨 0.98 边界）被拦截。
- **DTW 失败模式**（T15）：缺 `t_dtw`、`-1`、NaN 都失败并指出 token；显式 `--time offsets` 时输出 `timing_source: offsets`。
- **最终音频**（T23、T24）：带延迟补偿的链路，PCM 相对原歌偏移 0 采样；旧链路（不补偿）偏移 239 采样（约 5 ms）。普通音乐最终 AAC 为 −14.2 到 −14.4 LUFS、真峰值 −1.2 到 −1.3 dBTP，通过。高峰均比样本 3 次修正后为 −18.1 LUFS / −1.2 dBTP，判为失败、不发布并保留报告。静音和 0.3 秒短音频拒绝渲染并说明原因；加 `--allow-loudness-miss` 后发布并在报告里记录这是用户选择。
- **版本保护**（T26）：素材放在 MV 目录之外（绝对路径）时 manifest 照常记录指纹；v2 渲染失败、v3 渲染中途对整个进程组发 SIGINT（模拟终端 Ctrl-C）后，v1 目录全部文件哈希、`latest-passing.json` 都不变，没有遗留临时目录和候选文件；再写 v1 被拒绝。
- **输出锁**（T02）：测试进程自己持有输出锁时，渲染明确报"another run"并退出；锁释放后正常发布。

另外按 README 的快速开始，在仓库外的目录里用合成素材跑完：分析参考片 → 密抽 → 拆歌 → 逐字时间 → 复制生成脚本 → `--no-render` → 渲染 720p60。QA 全部通过，联系表里字卡在下一刀前退场。

## 真实素材回放（只读）

用一首真实歌曲（68 秒，中文歌词夹英文词）的现有材料做了只读回放，输出只写在临时目录，不进本仓库：

- **逐字时间**：两份带 DTW 的 whisper 输出（large-v3-turbo 与另一次 DTW 运行）都被新的 `lyric_chars.py` 接受；一份早期没加 `-nfa` 的输出（`t_dtw` 全为 −1）被拒绝并指出第一个 token，符合预期。与旧版生成的逐字时间相比，233 个单元里 3 个不同：两个是旧版的错位（英文词 `CPU` 被 ASR 拆成 `C`、`B` 后，旧版把它和前一个字各推后一格），一个是插值字移动了 0.12 秒。回放中还发现字母级对齐会把被听成同音字的英文词 `Bug` 错配到前面的字上，已改为按展示单元对齐（见 `references/conventions.md`），并把这两种情况写进 T17。两个模型之间差超过 0.12 秒的单元有 10 个，估计出来的单元 17 个——这只说明两次转写的一致程度，不代表真实唱点准确率。
- **剪辑表**：5 份当时实际渲染过的剪辑表（两种参考风格的正片和片尾，含 2K 版）在新校验器下全部通过，没有被新增的限制误拒；它们都没有 `avoid` 字段，校验器按设计给出"未检查禁用区间"的警告。
- **校验耗时**：正片剪辑表引用 13 条 2K（3184×1792、60 fps）素材，帧表需要完整解码。最初每次校验约 72 秒；改为多线程解码后首次约 15 秒，帧表缓存命中后不到 1 秒。

## 已知限制与未执行的验证

- 新流程没有在真实素材上完整渲染过成片；真实素材只做了上面的逐字时间和剪辑表校验回放。逐字时间的准确率没有对照真实唱点验证过。
- 没有带声音实时播放过任何成片；卡点、甩镜等的观感未经人工验证。
- 只在一台 macOS + FFmpeg 8.1 上测试。不支持硬链接的文件系统（exFAT、部分网络盘）上的发布走"持锁检查后重命名"的后备路径，没有实际在这类卷上测过。任务书里提到 FFmpeg 7.1.5 上 freeze 只出 1 帧，本机没有 7.1.5，修复按帧内容在 8.1 上验收。
- HDR 参考片只会报告不支持，没有色彩转换。
- `wipe_in` 的第二路 AVOID 检查是保守的（擦除完成前可能露出的帧都算），会多拦，不会漏拦。

## 后续项（任务书允许后置的）

- `wipe_in` 的遮罩仍逐帧写 PNG；可以改成流式。
- 全部渲染阶段的增量缓存、硬件编码。
- 多平台、多 FFmpeg 版本的测试矩阵。
- 复杂镜头语义识别、人物跟踪、自动画质评分；通用拍号识别和音乐结构理解；完整 HDR 色彩管理。
