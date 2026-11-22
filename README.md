# mv-replicate：照着参考 MV 的剪辑手法剪自己的 MV

一个给 AI 编程助手（Claude Code、Codex 等）用的 skill，附一组本机就能跑的脚本。

你有一首歌和一堆已经生成好的镜头（比如 AI 生成的 5–10 秒短片），又有一支喜欢的 MV。这个 skill 把参考 MV 的剪辑手法（切的密度、白闪、震屏、卡歌词的大字、甩镜、定格署名卡、调色、响度）拆成规则，再套到你自己的歌和镜头上，用 ffmpeg 和 Pillow 在本机渲染出来。

> **English summary.** An agent skill plus local scripts for re-cutting your own song and footage in the editing style of a reference music video. It turns the reference into rules (cut density, flashes, lyric-synced big text, whip pans, freeze-frame credit cards, grading, loudness), analyses your song's beats and per-character sung times (whisper DTW), builds a beat-indexed edit decision list and renders it with ffmpeg plus Pillow overlays (no `drawtext` needed). Docs are in Chinese.

## 它怎么做

1. **把参考片拆成规则，不抄时间码。** `analyze_ref.py` 量出切点、白闪、黑场、每段亮度和饱和度、响度和节拍，再配合 `frame_sheet.py` 密抽（0.1 秒一格）逐个看转场：怎么进、怎么出、多长、落在拍子哪里。结论写成规则，比如「换段落白闪」「主钩子上砸标题字」「最后一句副歌一拍一切」。
2. **拆自己的歌。** `analyze_song.py` 出节拍和小节相位；`whisper-cli` 的 DTW 时间戳加 `lyric_chars.py`，把每个字的唱点对到正确歌词上（whisper 常听错字，但时间是准的）。
3. **写按拍子落刀的剪辑表。** 每支 MV 一个生成脚本（从 `examples/make_edl.py` 起步）：`PLAN` 里每一刀写节拍序号，`AVOID` 拦住素材里的废帧，大字卡的每个字落在它被唱到的那一刻。
4. **渲染并自查。** `render_edl.py` 按剪辑表逐段裁切、加转场和叠层、调色、做响度（−14 LUFS / −1 dBTP），`lyric_layer.py` 渲整首的底部特效歌词。先出 720p 给人看，确认了再出 2K。

`SKILL.md` 是给 agent 读的完整方法，包括从多轮真实看片意见里总结出的默认做法和踩过的坑。

## 依赖

- **ffmpeg / ffprobe**（需要 `scdet`、`signalstats`、`ebur128`、`loudnorm` 滤镜；不需要 `drawtext`）。
- **Python 3.12**，装 `requirements.txt` 里的 librosa、pillow、numpy、soundfile。
- **whisper.cpp 的 `whisper-cli`**（macOS 可 `brew install whisper-cpp`），以及自己下载的 ggml 模型：主用 `large-v3-turbo-q5_0`，交叉核对用 `small`。可以用 whisper.cpp 仓库里的 [`models/download-ggml-model.sh`](https://github.com/ggml-org/whisper.cpp/tree/master/models) 下载，或从 [Hugging Face](https://huggingface.co/ggerganov/whisper.cpp) 手动下载。
- **字体**：仓库里不带字体，请从官方渠道下载并自行确认许可。
  - 正文粗黑体：默认找 `HarmonyOS_Sans_SC_Black.ttf`（[华为开发者设计资源](https://developer.huawei.com/consumer/cn/design/resource/)），使用前请阅读华为的许可条款。也可以换任何你有权使用的中文字体，用环境变量 `MV_REPLICATE_FONT` 指定。
  - 可爱展示字体（歌名、角色名）可选，例如站酷快乐体 [ZCOOL KuaiLe](https://fonts.google.com/specimen/ZCOOL+KuaiLe)、得意黑 [Smiley Sans](https://github.com/atelier-anchor/smiley-sans)，二者都是 SIL Open Font License 1.1。在剪辑表里用 `font` 字段指定路径。

## 安装

```bash
git clone https://github.com/santosli/mv-replicate.git
cd mv-replicate
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
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
python3 scripts/lyric_chars.py $W/song/whisper.json --lyrics $W/song/lyrics.json \
    --duration 68 --out $W/song/lyric-chars.json

# 3. 复制生成脚本，改 CLIPS / AVOID / PLAN，生成剪辑表并渲染 720p
cp examples/make_edl.py $W/make_edl_v1.py
$PY $W/make_edl_v1.py --work $W
```

生成脚本会写出 `$W/edl.json`，渲出 `$W/render/lyrics.mov` 和 `$W/render/mv-720p.mp4`。复制出去的生成脚本靠 `MV_REPLICATE_DIR` 找到本仓库的 `scripts/`。剪辑表的全部字段见 `scripts/render_edl.py` 开头的说明，每个脚本都有 `--help`。

## 限制

- 只复刻剪辑层面的手法。画进画面里的东西（速度线、角色踩拍的动作、作画层面的转场）、跟踪贴字、复杂特效复刻不了；参考片里有、你的素材里没有的镜头也变不出来。
- 节拍检测和切点检测都是起点，不是答案：动画和 MV 的闪光、运镜会让切点数不准，AI 生成的歌前奏速度可能不稳，都要人看图、听歌核对。
- 逐字唱点依赖 whisper 的 DTW 时间，哼唱和拟声听不出，只能均分估计（标为 guessed）。
- 渲染是逐段调用 ffmpeg、逐帧用 Pillow 画叠层，速度一般；2K、60 fps 的长片要等一阵。
- 目前只在 macOS（Apple Silicon）上实际用过；Linux 理论上可用，没测过。

## 版权和素材

- 代码以 MIT 许可发布，见 `LICENSE`。
- 仓库不包含任何参考视频、歌曲、生成的媒体或字体。参考 MV 只用于分析手法，不要把它的画面放进你的成片；你自己的歌、镜头、字体请确认有权使用。
