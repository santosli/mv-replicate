# 时间、取帧和产物约定

校验器、生成器、渲染器、抽帧和 QA 共用这些约定，实现都在 `scripts/mvr_common.py`。

## 时间

- 对外的时间字段一律是秒；源视频帧号从 0 开始，按解码后的显示顺序编号。
- 成片时间（film time）是成片里的秒；源时间（source time）是素材文件里的秒；局部动画时间是一个叠层从它自己的 `at` 起算的秒。`text_pop` 的 `pieces[].at`、`wobble`、`out` 用成片时间。
- 时间先四舍五入到素材流的 time base 刻度（`tick(t) = floor(t / tb + 1/2)`），再比较。十进制的 1/60 秒在浮点里不是精确值，不先取整，"正好落在某帧 PTS 上"就会选成前一帧。
- 所有数值必须是有限数。JSON 里出现 NaN / Infinity 会被拒绝；测不出的值写 `null`，并给出 `status` 和 `reason`。

## 成片帧数

- `Q(t) = floor(t × fps + 1/2)`，fps 可以是 `30000/1001` 这样的分数。
- 第 k 段的帧数是 `Q(at + dur) − Q(at)`，按累计边界量化，不按每段时长单独舍入再相加。所以整片帧数恒等于 `Q(duration)`，分段再多也不会累计漂移。
- 叠层也落在同一张帧格上：从 `Q(at)` 帧开始，共 `Q(at + dur) − Q(at)` 帧。`at` 为负的叠层（片头预滚）保留自己的动画时钟，0 秒之前的部分裁掉，不整体平移。

## 一段素材显示哪些源帧

- 输出第 k 帧显示源时间 `from + k × speed / fps` 时屏幕上的那一帧，也就是 PTS 不大于该时间的最后一帧（在一个 time base 刻度之内）。渲染图用 `setpts=trunc((PTS − S) / speed)` 加 `fps=…:round=up:start_time=0` 实现，`validate_edl.py` 用同一个模型预先算出每一帧；在 B 帧、30000/1001、非零起始 PTS 和 VFR 素材上与 FFmpeg 实测逐帧一致（`tests/run_tests.py T06`）。
- `end` 锚定：`from = end − dur × speed`。
- `freeze`：整段保持 `from` 时刻屏幕上的那一帧。
- `reverse`：先精确选出上面那组帧，再倒序；不会把解码多读的帧带进片头。
- 源素材不够长时直接报错，不偷偷挪动选区。
- `--at t`（frame_sheet）和 `freeze.from = t` 用同一个规则。

## AVOID（禁用区间）

- 区间是半开的 `[start, end)`，单位是源时间秒。
- 一个源帧只要在区间内任何时刻在屏幕上，就算禁用：`pts < end` 且下一帧的 `pts > start`。所以区间边界不和帧对齐时，跨边界的那一帧也算禁用；`[a, b)` 和 `[b, c)` 正好相接时互不影响。
- 检查的是成片里实际显示的每一个源帧，覆盖主素材 `src` 和 `wipe_in` 的第二路（第二路按擦除完成前可能露出的全部帧保守检查）。
- 素材按文件身份比较（设备号 + inode），`clips/b.mp4`、`./clips/../clips/b.mp4` 和指向它的符号链接是同一个素材。
- 禁用区间写在 EDL 的 `avoid` 字段里，渲染入口每次都会检查；EDL 没有 `avoid` 时，校验器和报告会明确写"未提供禁用区间、未检查"。

## 逐字时间（lyric_chars.py）

- whisper 的 `t_dtw` 单位是百分之一秒，`offsets` 单位是毫秒，各只换算一次。
- 默认模式要求每个文本 token 都有有限且不小于 0 的 `t_dtw`，否则停下并指出是哪个 token；`--time offsets` 是显式选择，输出里记 `timing_source`。
- 歌词和 ASR 都切成展示单元（一个汉字，或一个完整的英文词/数字）再对齐：相同得 2 分；英文词和 ASR 的英文片段一方是另一方的前缀（如 `Hello` / `hel`、`CPU` / `C`）得 1 分；其余 0 分（同音字照样借到时间）；空位 −1。每个歌词单元取与它配对的 ASR 单元的时间。只按字母逐个对齐会把 `Bug` 的三个字母错配到前面的同音字上，所以不这样做。
- 每个字都记录来源：`asr`（直接来自 whisper）、`split`（多字 token 拆开推算）、`interpolated`（在可靠的前后时间之间线性补）、`spread`（整句均分）、`override`（手工设定）。除 `asr` 和 `override` 外都标 `guessed`。
- 时间在同一行内、跨行都不减；修复只用相邻的可靠时间和句子边界，绝不把时间排序后重新分给文字。可靠时间本身顺序冲突时默认停止。
- 起唱容差（默认 0.6 秒）写在输出的 `onset_tolerance` 里。
- 两个模型的结果一致，只说明一致，不能证明就是真实唱点。

## 输出与版本

- 每次渲染在 `work_dir` 里新建一个只属于本次运行的子目录，结束只删这个子目录。
- 成片先写成输出目录里的隐藏候选文件，QA 通过才发布；已有的输出默认拒绝覆盖，`--replace` 也只在新候选通过后原子替换。同一个输出由文件锁互斥，进程退出时锁自动释放（锁文件 `.<文件名>.lock` 会留在目录里，可以删）。
- 报告：成功写 `<名>.qa.json`，失败写 `<名>.failed-<运行号>.qa.json`；字段有 `schema_version`、`run_id`、`input_fingerprints`、`tool_versions`、`targets`、`measurements`、`tolerances`、`checks`（每项 pass / fail / skipped）、`warnings`、`status`。
- 生成器每个版本一个目录 `versions/<版本>/`，里面是该版的 EDL、歌词图层、成片、QA 报告、联系表和 `manifest.json`；已有成片的版本不再改动。`versions/latest-passing.json` 只在机器 QA 通过后更新。机器 QA 通过不等于用户认可，用户认可记在 manifest 的 `review` 里。

## 响度

- 默认目标 −14 LUFS、真峰值 −1 dBTP；容差默认综合响度 ±0.5 LU、真峰值不超过目标 +0.1 dB，可在 EDL 的 `qa` 字段里改。
- 测量的是最终 AAC 解码后的结果，不是编码前的 WAV。最多修正 3 次，仍不达标就失败、不发布，保留测量报告；`--allow-loudness-miss` 会发布并在报告里记录偏差和这是用户的选择。
- 限幅器 4 倍过采样、开延迟补偿，PCM 与原歌的时间偏移为 0 采样（`T23`）；AAC 的编码延迟由解码器按 edit list 去掉，QA 里 PCM 与成片的偏移单独测。
- 静音或短于 0.4 秒的音频测不出综合响度，状态写 `unmeasurable`，不会算出 NaN 增益。
