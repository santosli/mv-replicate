# mv-replicate：照着参考 MV 的剪辑手法剪自己的 MV

一个给 AI 编程助手（Claude Code、Codex 等）用的 skill，附一组本机就能跑的脚本。

你有一首歌和一堆已经生成好的镜头（比如 AI 生成的 5–10 秒短片），又有一支喜欢的 MV。这个 skill 把参考 MV 的剪辑手法（切的密度、白闪、震屏、卡歌词的大字、甩镜、定格署名卡、调色、响度）拆成规则，再套到你自己的歌和镜头上，用 ffmpeg 和 Pillow 在本机渲染出来。

> **English summary.** An agent skill plus local scripts for re-cutting your own song and footage in the editing style of a reference music video. It turns the reference into rules (cut density, flashes, lyric-synced big text, whip pans, freeze-frame credit cards, grading, loudness), analyses your song's beats and per-character sung times (whisper DTW), builds a beat-indexed edit decision list, validates it, renders it with ffmpeg plus Pillow overlays (no `drawtext` needed) and only publishes a film that passes machine QA. Docs are in Chinese.

## 它怎么做

1. **把参考片拆成规则，不抄时间码。** `analyze_ref.py` 量出切点、白闪、黑场、每段亮度和饱和度、响度和节拍，再配合 `frame_sheet.py` 密抽（0.1 秒一格）逐个看转场：怎么进、怎么出、多长、落在拍子哪里。结论写进风格规则表（`examples/style-spec.example.json`）。
2. **拆自己的歌。** `analyze_song.py` 出节拍和小节相位；`whisper-cli` 的 DTW 时间戳加 `lyric_chars.py`，把每个字的唱点对到正确歌词上，并记下每个时间是直接测到的还是估计的。
3. **写按拍子落刀的剪辑计划。** `examples/make_edl.py` 读你的 `project.json`：`plan` 里每一刀写节拍序号，`avoid` 拦住素材里的废帧，大字卡的每个字落在它被唱到的那一刻、在下一刀之前退场。
4. **校验、渲染、QA。** 计划先过 `validate_edl.py`，`render_edl.py` 再逐段裁切、加转场和叠层、调色、做响度（−14 LUFS / −1 dBTP），成片过 `qa_render.py` 才发布到 `versions/<版本>/`。先出 720p 给人看，确认了再出 2K。

`SKILL.md` 是给 agent 读的完整流程；`references/` 里是时间约定、默认偏好和一次实际使用的案例。

## 依赖

- **ffmpeg / ffprobe**（需要 `scdet`、`signalstats`、`ebur128`、`alimiter` 等滤镜和 libx264、aac、qtrle 编码器；不需要 `drawtext`）。`scripts/doctor.py` 会逐项检查。
- **Python 3.12**，装 `requirements.txt` 里的 librosa、pillow、numpy、soundfile。
- **whisper.cpp 的 `whisper-cli`**（macOS 可 `brew install whisper-cpp`），以及自己下载的 ggml 模型：主用 `large-v3-turbo-q5_0`，交叉核对用 `small`。可以用 whisper.cpp 仓库里的 [`models/download-ggml-model.sh`](https://github.com/ggml-org/whisper.cpp/tree/master/models) 下载，或从 [Hugging Face](https://huggingface.co/ggerganov/whisper.cpp) 手动下载。只渲染不需要它。
- **字体**：仓库里不带字体，请从官方渠道下载并自行确认许可。
  - 正文粗黑体：默认找 `HarmonyOS_Sans_SC_Black.ttf`（[华为开发者设计资源](https://developer.huawei.com/consumer/cn/design/resource/)），使用前请阅读华为的许可条款。也可以换任何你有权使用的中文字体，用环境变量 `MV_REPLICATE_FONT` 指定。
  - 可爱展示字体（歌名、角色名）可选，例如站酷快乐体 [ZCOOL KuaiLe](https://fonts.google.com/specimen/ZCOOL+KuaiLe)、得意黑 [Smiley Sans](https://github.com/atelier-anchor/smiley-sans)，二者都是 SIL Open Font License 1.1。在剪辑计划里用 `font` 字段指定路径。

## 安装

```bash
git clone https://github.com/santosli/mv-replicate.git
cd mv-replicate
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/doctor.py --task render --task analyze
```

作为 skill 使用时，把整个目录放进你的 agent 读取 skill 的位置（例如 Claude Code 的 `~/.claude/skills/mv-replicate/` 或项目里的 `.claude/skills/`），`SKILL.md` 在目录根上。

## 快速开始

下面在本仓库目录里执行；工作目录 `W` 可以放在任何地方（建议放在本仓库之外）。

```bash
export MV_REPLICATE_DIR=$PWD
PY=$PWD/.venv/bin/python
W=~/mv-work/my-mv
mkdir -p $W/song $W/clips
# 放进：$W/song/song.wav（你的歌）、$W/clips/*.mp4（你的镜头）、$W/reference.mp4（参考 MV，只用于分析）

# 1. 拆参考片：数字 + 1 秒一格抽帧图；转场要再密抽着看
$PY scripts/analyze_ref.py $W/reference.mp4 --out $W/analysis/ref
$PY scripts/frame_sheet.py $W/reference.mp4 --out $W/analysis/ref/dense.jpg --every 0.125 --range 0-15 --cols 12 --cell 160

# 2. 拆自己的歌：节拍，再加逐字唱点（歌词格式见 examples/lyrics.example.json）
$PY scripts/analyze_song.py $W/song/song.wav --lyrics $W/song/lyrics.json --out $W/song
ffmpeg -i $W/song/song.wav -ar 16000 -ac 1 $W/song/song16k.wav
whisper-cli -m /path/to/ggml-large-v3-turbo-q5_0.bin -dtw large.v3.turbo -nfa -l zh -ojf \
    -of $W/song/whisper -f $W/song/song16k.wav
$PY scripts/lyric_chars.py $W/song/whisper.json --lyrics $W/song/lyrics.json \
    --duration 68 --out $W/song/lyric-chars.json

# 3. 写剪辑计划（从示例改起），先只生成并校验 EDL，再渲染第一版
cp examples/make_edl.py $W/make_edl.py
cp examples/project.example.json $W/project.json      # 改 clips / avoid / plan / title
$PY $W/make_edl.py --work $W --project $W/project.json --preset presets/default.json --version v1 --no-render
$PY $W/make_edl.py --work $W --project $W/project.json --preset presets/default.json --version v1
```

第一版的产物都在 `$W/versions/v1/`：`edl.json`、`lyrics.mov`、`mv-720p.mp4`、`mv-720p.qa.json`（机器 QA 报告）、`sheet.jpg`（1 秒一格联系表）、`manifest.json`（输入指纹、配置、工具版本）。下一刀用 `--version v2`，v1 不会被改动。复制出去的生成脚本靠 `MV_REPLICATE_DIR` 找到本仓库的 `scripts/`。

单独使用的入口（每个都有 `--help`）：

```bash
$PY scripts/doctor.py --edl $W/versions/v1/edl.json --root $W        # 环境 + 这个 EDL 用到的字体和素材
$PY scripts/validate_edl.py $W/versions/v1/edl.json --root $W         # 只校验，不渲染
$PY scripts/render_edl.py $W/versions/v1/edl.json --root $W           # 校验 → 渲染 → QA → 发布
$PY scripts/qa_render.py $W/versions/v1/mv-720p.mp4 --edl $W/versions/v1/edl.json
$PY scripts/compare_lyric_timing.py turbo.json small.json             # 两个模型的逐字时间对比
```

## 测试

```bash
.venv/bin/python tests/run_tests.py          # 全部（约 1 分钟）
.venv/bin/python tests/run_tests.py T05 T13  # 指定编号
```

测试素材全部在运行时合成（逐帧编号的彩色视频、VFR 视频、合成的歌和 whisper JSON），不下载任何东西；每个测试名以编号开头（T01–T27），失败时能直接对上。没有 ffmpeg 时媒体测试会标为 skipped。

## 从旧版本迁移

- `work_dir` 现在是临时目录的**父目录**：每次渲染在里面新建一个子目录，结束只删这个子目录，不再清空 `work_dir` 本身。
- 已存在的输出默认拒绝覆盖；要替换加 `--replace`，而且新片过 QA 后才替换。
- 渲染入口会先校验 EDL；不合法的 EDL（段落不连续、负时长、NaN、素材不够长、输出指向输入）不再渲染。
- 段落帧数改为按累计边界量化（`Q(at + dur) − Q(at)`），整片帧数恒等于 `Q(duration)`。
- 源帧选择改为"输出第 k 帧显示源时间 `from + k × speed / fps` 时屏幕上的那一帧"；`reverse` 精确倒放所选帧，`freeze` 保持 `from` 时刻的那一帧。
- `frame_sheet.py --at t` 选 PTS 不大于 t 的最后一帧（以前按 `-ss` 四位小数取，可能拿到下一帧）；`--frames` 按真实解码帧号；任何请求失败默认非零退出，`--lenient` 可以只发布找到的部分。
- `lyric_chars.py` 默认要求 DTW 时间完整，缺了就报错，不再静默用 offsets；输出每个字多了 `source`；`guessed` 现在也包括多字 token 拆开推算的时间。
- `lyric_layer.py --yield TEXT@T` 只让唱到的那一次让位（以前所有含 TEXT 的句子都被截断）；有歧义时加 `#序号`。
- `analyze_song.py` 检测不到拍时直接失败，可用 `--beats` 给手工节拍；`--beats-per-bar`、`--bar-phase` 可改。
- 限幅器改为 4 倍过采样并开启延迟补偿；响度按最终 AAC 测量，达不到目标默认失败。
- `examples/make_edl.py` 改为按版本输出到 `versions/<版本>/`，计划可以放在 `project.json`；`AVOID` 写进 EDL，由渲染器检查。旧的 `--work` 用法仍可运行，默认版本是 `v1`。

## 限制

- 只复刻剪辑层面的手法。画进画面里的东西（速度线、角色踩拍的动作、作画层面的转场）、跟踪贴字、复杂特效复刻不了；参考片里有、你的素材里没有的镜头也变不出来。
- 节拍检测和切点检测都是起点，不是答案：动画和 MV 的闪光、运镜会让切点数不准；小节相位是假设，不是识别出来的强拍。
- 逐字唱点依赖 whisper 的 DTW 时间；双模型一致只说明一致，不证明就是真实唱点。哼唱和拟声听不出，只能均分估计。
- HDR（PQ / HLG）参考片的颜色分析不支持。
- 渲染是逐段调用 ffmpeg、逐帧用 Pillow 画叠层，速度一般；没有增量缓存和硬件编码。
- 只在 macOS（Apple Silicon）+ FFmpeg 8.1 + Python 3.12 上跑过测试；其他平台和 FFmpeg 版本没有验证。

## 版权和素材

- 代码以 MIT 许可发布，见 `LICENSE`。
- 仓库不包含任何参考视频、歌曲、生成的媒体或字体。参考 MV 只用于分析手法，不要把它的画面放进你的成片；你自己的歌、镜头、字体请确认有权使用。
