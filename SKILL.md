---
name: mv-replicate
description: 照着一支参考 MV 的剪辑手法，用自己的歌和已生成的镜头剪出自己的 MV：拆参考片（切点、段落、转场、大字卡、色调、响度）、拆自己的歌（节拍、逐字唱点）和镜头（可用段、废帧），写成按拍子落刀的剪辑表，先校验再用本机 ffmpeg + Pillow 渲染，成片过机器 QA 后才发布；包括卡点、剪废帧、转场、画面字、底部特效歌词、歌名卡、片尾字幕和彩蛋、调色、响度。换一支参考片再剪一版做对比也用它。
---

# 复刻 MV 剪辑手法

找一支喜欢的 MV，学它的剪辑手法，剪自己的歌和镜头。

下文 `$MVR` 指本 skill 所在目录，`$PY` 指装好依赖的 Python（见 README「安装」），`<work>` 指这支 MV 的工作目录。时间、取帧、禁用区间和产物的精确约定见 `references/conventions.md`。

## 什么时候用、需要什么

- 用户有一首歌（wav）、一批已经生成好的镜头（mp4）和一支参考 MV，想照参考片的剪法剪一版；或者想换一支参考片再剪一版对比。
- 需要的输入：歌；镜头；参考片（只用来分析）；逐句歌词和每句起唱时间（格式见 `examples/lyrics.example.json`）；要做逐字效果时还需要 whisper 模型。
- 工作目录约定：`<work>/song/`（歌和分析结果）、`<work>/clips/`（镜头）、`<work>/project.json`（这支 MV 的剪辑计划和记录）、`<work>/versions/<版本>/`（每一版的产物）。

## 必须遵守的规则

1. **不动输入**：歌、镜头、参考片、字体、EDL 都不会被覆盖或删除。`render_edl.py` 只删自己新建的临时子目录，输出指向任何输入都会被拒绝。
2. **先校验再渲染**：`validate_edl.py` 不通过就不渲染；渲染入口自己也会先跑同样的校验。校验失败说清是哪一段、哪个字段，然后改计划，不要绕过。
3. **时间必须有效**：时间是有限数，歌词时间不减，素材不够长就报错。逐字时间来自 whisper DTW（`-dtw … -nfa`），缺了就停，不偷偷换成 offsets。
4. **废帧不进成片**：用户否掉的段、穿模、坏帧写进 `avoid`（源时间 `[start, end)`），渲染器会拒绝任何在这些区间里显示过的源帧，包括 `wipe_in` 的第二路。
5. **版本保留**：每一版一个 `versions/<版本>/`；已有成片的版本不再改动，下一刀用新版本号。已通过的版本在新版过审前不替换。
6. **成片必须过机器 QA**：帧数、尺寸、帧率、音频解码和连续性、响度（最终 AAC）、音画偏移。不通过就不发布，原有成品保持不变。机器 QA 通过 ≠ 用户认可，审美要用户看过才算。
7. **只说做了的**：没带声音实时播过的就写"未带声音播放验证"；双模型一致不等于唱点准确；EDL 写了某个效果不等于视觉上复刻到了。
8. **花钱的事先问**：本机渲染不花钱；补镜头等付费生成先说清成本再问。参考片要从网上下载时，先问用户来源、文件名和大小。

## 偏好和项目记录分开

- 通用规则在上面。
- 剪辑偏好（片头不留黑场、字卡到唱点、署名只留一处、底部歌词样式参数、响度目标等）是**预设**：`presets/default.json` 和 `references/house-preferences.md`。当前任务里用户明确说的优先；预设不替用户决定每支 MV 都要彩蛋或固定帧率。
- 这支 MV 的例外、选中的样式、手工改过的字时间、用户认可的版本，记在 `<work>/project.json` 的 `records`、`song/lyric-overrides.json` 和各版本的 `manifest.json` 里。
- 新的看片意见先进项目记录；只有用户明确说"以后都这样"，才改预设或本文件。

## 步骤

0. **检查环境**：`$PY $MVR/scripts/doctor.py --task render --task analyze`；要逐字时间再加 `--task asr --whisper-model <模型>`。缺的东西会写 `fail`，可选的写 `skipped` / `unavailable`。
1. **拆参考片**：`$PY $MVR/scripts/analyze_ref.py <参考片> --out <work>/analysis/ref`。8/10 bit、limited/full range 的 SDR 会先统一再统计；HDR 的颜色分析会写"不支持"。参考片没有音轨时视觉分析照做，音频写 unavailable。
   - **必须密抽看转场**：1 秒一格只看得出段落和色调。全片按 0.1–0.125 秒一格抽（`frame_sheet.py --every 0.125 --range <每 15 秒一段> --cols 12 --cell 160`），逐个刀口写清：怎么进、怎么出、多长、由什么带出来、落在拍子哪里、出现几次。
   - 写一份风格规则表（格式见 `examples/style-spec.example.json`）：每条规则有 id、参考片区间、音乐功能、手法和参数、目标段落、验收证据、状态。和预设冲突的手法（黑场起等）记为 `preference_override`，做不到的记为 `unsupported` 或 `missing_footage`。
2. **拆自己的歌**：
   - 节拍：`$PY $MVR/scripts/analyze_song.py <work>/song/song.wav --lyrics <歌词.json> --out <work>/song`。小节拍数 `--beats-per-bar`（默认 4）；小节相位是假设，输出里会写来源（歌词句首投票 / 起音强度 / 手工 `--bar-phase`）。检测不到拍或检测错了，用 `--beats` 给手工节拍，不要按等间隔外推。
   - 逐字唱点：转 16 kHz 单声道后 `whisper-cli -m <large-v3-turbo 模型> -dtw large.v3.turbo -nfa -l zh -ojf -of <work>/song/whisper-turbo -f song16k.wav`，再 `$PY $MVR/scripts/lyric_chars.py <work>/song/whisper-turbo.json --lyrics <歌词.json> --duration <秒> --out <work>/song/lyric-chars.json`。哼唱句加 `--spread <文字>`。
   - 交叉核对：用 small 模型（`-dtw small`）再出一份，`$PY $MVR/scripts/compare_lyric_timing.py turbo.json small.json` 列出差超过 0.12 秒的字和所有估计出来的字，逐个听。
   - 人工改字时间：`lyric_chars.py … --override-template <work>/song/lyric-overrides.json` 生成模板，把要改的字放进 `items`，之后每次加 `--overrides`。歌词文件变了，旧的修订会被判为过期并停下。
3. **清点镜头**：所有素材按 0.5 秒一格拼总表（`frame_sheet.py --every 0.5`），记下每条素材的内容和时间；废帧、穿模、用户否掉的镜头写进 `project.json` 的 `avoid`。
4. **写剪辑计划**：把 `$MVR/examples/make_edl.py` 复制到 `<work>`，计划写在 `<work>/project.json`（格式见 `examples/project.example.json`），改刀只改计划，不手改生成的 EDL。
   - **刀口**：`plan` 里每一刀用节拍序号定位，段落对到风格规则表上。字段见 `render_edl.py` 开头的说明。
   - **慢放**会出重复帧，只在安静段用；不用 minterpolate 补帧。镜头不够一拍长就用 `{"end": 秒}` 锚在素材尾。
   - **画面字**：`title` 指定哪句、哪几个字、放哪；每个字在它被唱到时弹出。生成器按**实际的下一刀**算退场，保证淡出在刀口前完成；字和刀口靠得太近会直接报冲突，挪刀或挪字。
   - **底部歌词**：新歌或想换样式时，先用 `lyric_layer.py --start <秒> --duration 8` 出几种样式的样片让用户挑。标题字就是那句歌词时用 `--yield 文字@秒`，只让被唱到的那一次让位；有重复歌词时按提示加 `#序号`。
   - **歌名卡、片尾字幕、彩蛋**：做法见 `references/house-preferences.md`，彩蛋先问。
5. **生成和渲染**：`MV_REPLICATE_DIR=$MVR $PY <work>/make_edl.py --work <work> --project <work>/project.json --version v1 [--preset $MVR/presets/default.json]`。先出 1280×720 给用户看；2K 用新版本号加 `--size 2560x1440`。只想看 EDL 用 `--no-render`（照样会校验）。直接渲染已有 EDL：`$PY $MVR/scripts/render_edl.py <edl.json> --root <work>`。
6. **自查后再交**：
   - 读 `versions/<版本>/mv-<高>p.qa.json`：`status` 必须是 `pass`；响度若靠 `--allow-loudness-miss` 放行，汇报里写实际值和偏差。
   - `frame_sheet.py --every 1` 看全片；改过的地方（字效、歌词、竖框、白闪、片头、片尾）`--dense <秒> --span 0.5 --step 1` 密抽；需要取证时加 `--json`。
   - 按风格规则表逐条更新状态和证据：哪条复刻了、哪条近似、哪条没做、为什么。卡点、甩镜、快切的代表片段要带声音播放验证，没播放过的明确写未验证。
   - 汇报写清版本目录、QA 结果、未验证项；用户认可后再在 manifest 的 `review` 里记下。

## 换一支参考片再剪一版做对比

对比的是剪辑手法，所以只换和参考片有关的部分，其余照旧，差别才看得出来。

- **照旧**：歌、逐字时间和人工修订、`avoid`、底部歌词图层（同样式同颜色）、歌名卡、片尾字幕和彩蛋、响度、预设。
- **换掉**：段落划分、切的密度和长镜头分布、转场（闪白、震屏、推近、竖框、甩镜、定格）、大字卡放在哪句、调色（亮度和饱和度跟新参考片，颜色跟本片配色走）。
- **做法**：新参考片照步骤 1 拆，出新的风格规则表；复制出新的项目文件（如 `project-<新参考>.json`），只重写 `plan`、转场参数、字卡位置和 `grade`；用新版本号输出。交片时附对比表：按歌的段落，两版各用了什么手法、切了几刀。
- 用户选定一版后，在项目记录里写下选了哪版和原话；没选中的版本留着。一次实测的经过见 `references/case-study.md`。

## 失败了怎么办

| 现象 | 处理 |
|---|---|
| `validate_edl` / `make_edl --no-render` 报错 | 按报出的段落和字段改计划；素材不够长就换起点或换镜头 |
| 报 `avoided range` | 这一刀碰到了禁用区间，换起点或换镜头，不要删禁用区间 |
| `lyric_chars` 报 `t_dtw` 无效 | whisper 没开 DTW 或没加 `-nfa`，重跑 whisper；确实只能用 offsets 时显式加 `--time offsets` 并在汇报里说明 |
| `lyric_chars` 报时间顺序冲突 | 用 `--overrides` 手工定时间；或确认后加 `--drop-conflicting-anchors` |
| 渲染报 `already exists` / `another run` | 用新版本号；或确认要替换后加 `--replace`（新片过 QA 后才替换） |
| QA 失败 | 不发布；读 `*.failed-*.qa.json`，看哪项失败、实测值是多少 |
| 响度修正 3 次仍不达标 | 默认失败；告诉用户实测值，由用户决定是否 `--allow-loudness-miss` |

## 交付标准

- 版本目录齐全：EDL、成片、QA 报告（`status: pass`）、联系表、manifest。
- 风格规则表的状态按实际成片更新过。
- 汇报写清：哪些规则做到了、哪些近似或没做；哪些没带声音播放验证；逐字时间里有多少是估计出来的。
- 用户认可前不替换已通过的旧版。

## 已知的坑

- 不要把 loudnorm 放进视频滤镜图（动态模式曾让开头一段音频消失）；`render_edl.py` 先单独渲 WAV。
- whisper 不要用 token offsets；`-dtw` 必须配 `-nfa`。
- 混音里起音点很密，不能拿来证明唱点对；用两个模型交叉核对，或看人声频段的能量起点。
- Pillow 的 `alpha_composite` 不接受出界坐标，用 `render_edl.py` 的 `paste_rgba`。
- 改了共用参数（比如字号同时影响歌名卡）要整条重渲。

## 分工

- 剪辑计划第一版由 agent 按规则剪完整，不每一刀都问；用户看片挑毛病、定砍哪段换哪个。
- 样式样片、批量 QA 这类细活可以交给子 agent：简报写清目标、输入、输出路径、不许动的文件和自查项。主 agent 看过结果再交用户。
