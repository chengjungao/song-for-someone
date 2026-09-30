# song-for-someone

> 给某个人的一首歌。

在你自己电脑上出一首完整的歌。不联网、不要额度、不限时长、不传任何数据出去。

```bash
sfs make --style folk --lyrics-file lyrics.txt -o 给妈妈的歌.mp3
```

```text
风格： 温暖的中文流行民谣，木吉他为主，钢琴点缀，女声，情感真挚，中速，副歌有起伏
时长： 2.0 分钟 ｜ 语言：zh ｜ 步数：8 ｜ 版本数：1
输出： songs\给妈妈的歌.mp3

任务已提交：b2190d64-b55c-48ce-b3da-f4e78a99c613

  [     5s] 生成中 .
  [    10s] 生成中 .

生成完成，用时 15.1 秒。

  songs\给妈妈的歌.mp3 ｜ 2.1 MB
    77 BPM ｜ D major ｜ 120 秒 ｜ acestep-v15-turbo
```

上面这段是真的跑出来的，不是在服务器上挑了个好看的例子。硬件是一张 RTX 4080。

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

---

## 这个项目是什么

它本身不生成音乐。真正干活的是 [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)（StepFun 与 ACE Studio 开源的音乐生成模型，MIT 许可）。

这个项目补的是它落地时缺的那几块：

| 缺什么 | 这里有什么 |
|---|---|
| Windows 上 triton 3.3.1 必崩，服务起不来 | 一行命令打补丁，附完整根因分析 |
| 只有裸 HTTP 端点，调用要手搓 urllib 加轮询 | 标准库实现的客户端，以及一个 `sfs` 命令行 |
| 歌词写得好不好没人管，写完才知道废了 | 提交前跑一遍歌词结构体检 |
| 中文场景的实操口径散落在各处 | 中文风格模板、行宽判据、编码处理 |
| 性能数据没人给 | 一张 4080 上的时间账与显存账 |

**一点第三方依赖都没有。** 全部用 Python 标准库实现。clone 下来就能跑，不用先建环境再装包。

---

## 先说门槛

这个项目对读者不友好，我必须提前说清楚：

- **需要一张 N 卡**，显存 6GB 起步（DiT 与 LM 同时在卡上）。没有的话会落到 CPU，慢一个数量级
- **需要下约 10GB 模型权重**，首次启动自动下，国内建议走 ModelScope
- **需要装好 ACE-Step 1.5 本体**。Windows 有官方便携包（2.4GB），免装 Python 环境
- 出歌本身 10 到 30 秒，取决于显卡；但**启动时要先花约 1 分钟**把模型载入显存（首次运行还要下模型，更久）

如果你想要的是「打开网页点一下就有歌」，用豆包、剪映这类 App 更合适。这个项目的价值在于：**歌在你自己的机器上，想跑几遍跑几遍，时长你说了算，歌词不用传给任何人。**

---

## 快速开始

### 不想敲命令？双击就行（推荐）

**双击 `启动.bat`**（Windows；其他系统执行 `python start.py`）。它会把两件事一起做完：

```text
双击 启动.bat
  → 找到 ACE-Step 便携包，用它自带的 Python 把引擎起起来
  → 浏览器自动打开网页
  → 填歌词 + 选风格
  → 点「开始出歌」
```

首次运行要下约 10GB 模型，可能等十几分钟；之后每次冷启动约 1 分钟。控制台会一直
报进度，不会让你干等。

**关掉那个窗口，界面和引擎一起停，显存随即释放。** 不想每次等这 1 分钟，就用便携包
根目录的 `启动引擎.bat` 单独把引擎开着，`启动.bat` 检测到已有引擎会直接复用。

只想要界面、不想动引擎：`python start.py --no-engine`。
便携包不在常见位置：`python start.py --engine-root D:\你的目录`。

它和命令行是**同一套能力**，文件也都存到同一个 `songs/` 里。图文版说明见
[docs/04-图形界面使用.md](docs/04-图形界面使用.md)。

### 第 1 步：备好 ACE-Step 1.5

Windows 用户下载官方便携包，解压到一个固定目录：

```text
https://files.acemusic.ai/acemusic/win/ACE-Step-1.5.7z
```

路径随意，`启动.bat` 会自己找（常见落点是 `D:\Works\ACE-Step-1.5-portable`）。

> **不要用官方的 `start_api_server.bat` 起服务。** 它判断的目录名是
> `python_embedded`，而便携包实际目录叫 `python_embeded`（少一个 `n`，
> 上游自己拼错了），于是判断永不成立，它会转去用 `.venv` —— 那是个没装
> torch 的空壳，必然起不来。便携包里放了一份修正版 `启动引擎.bat`，
> 只想起引擎不起界面时用它。

其他平台看[上游的安装文档](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md)。

引擎监听 `http://127.0.0.1:8001`。

### 第 2 步：确认环境没问题

```bash
git clone https://github.com/chengjungao/song-for-someone.git
cd song-for-someone
python -m song_for_someone doctor
```

不必安装，不必建虚拟环境。有个 Python 3.9 以上就行。

自检会自己去找便携包（当前目录、用户主目录、各盘根目录，以及里面名字带
`ACE-Step` 的文件夹）。装在不常见的位置时再加 `--package-root D:\你的目录`。

输出长这样：

```text
环境自检
==============================================================
[ OK ] Python 版本
        3.13.14 ｜ Windows-11-10.0.26200-SP0

[ OK ] ACE-Step 服务
        http://127.0.0.1:8001 ／health 正常

[ OK ] 显卡
        NVIDIA GeForce RTX 4080 ｜ 显存 12944/16376 MiB ｜ 驱动 616.92

[ OK ] ACE-Step 便携包
        D:\Works\ACE-Step-1.5-portable

[ OK ] 便携包自带 Python
        python_embeded\python.exe ｜ 带 torch

[ OK ] triton 补丁
        已装 ｜ D:\Works\ACE-Step-1.5-portable\python_embeded\Lib\site-packages\sitecustomize.py

[ OK ] diffusers 导入
        diffusers 0.36.0 ｜ torch 2.7.1+cu128 ｜ CUDA 可用

[ OK ] 模型文件
        2/2 齐 ｜ D:\Works\ACE-Step-1.5-portable\checkpoints ｜ 约 9.4 GB

[ OK ] 输出目录
        songs ｜ 剩余 1150.7 GB
==============================================================
结论：环境没问题，可以出歌了。
```

哪一项是「失败」，按它给的提示处理。**Windows 上十有八九会卡在 diffusers 导入**，见下一节。

### 第 3 步：出歌

```bash
# 看看有哪些风格模板
sfs styles

# 写歌词（结构标签很重要，模型靠它切段落）
cat > lyrics.txt <<'EOF'
[Verse 1]
那年冬天你把围巾留给我
自己缩着脖子走过三条街

[Chorus]
生日快乐 我唱得不算好
但我记得你所有的样子
EOF

# 体检，再看要不要改
sfs check lyrics.txt

# 出歌
sfs make --style folk --lyrics-file lyrics.txt -o 给妈妈的歌.mp3
```

不想安装也可以用 `python -m song_for_someone` 代替 `sfs`：

```bash
python -m song_for_someone make --style folk --lyrics-file lyrics.txt
```

想装成命令：

```bash
pip install -e .
```

---

## Windows 上那个坑

这是本项目最实在的一块，单独讲。

在 Windows 上跑 ACE-Step，服务经常起不来，报错是：

```text
PermissionError: [WinError 5] Access is denied: '...\cuda_utils.pyd'
```

**根因**在 `triton/runtime/cache.py` 的 `FileCacheManager.put()`。它把编译产物先写到临时文件，再用 `os.replace` 换到最终位置。Windows 上 `os.replace` 会因为目标文件被占用而失败；此时 `put()` 的兜底只 try 了 `os.remove(temp_path)`，而**刚编译出来的 `cuda_utils.pyd` 正被进程加载着锁死**，`remove` 同样抛 `WinError 5`。异常于是冒出 `put()`，一路顶到调用方，也就是 diffusers 的导入链。

它会自我维持：缓存文件永远写不进去，于是**每次启动都重新编译一遍**，每次都在同一个地方失败。

**换缓存目录没用。** `TRITON_CACHE_DIR` 只是换个地方写，锁还在。脱离沙箱也一样，这是真实的 Windows 文件锁语义。

**这里有个补丁**：往 site-packages 放一个 `sitecustomize.py`（Python 启动时自动 import），把 `put()` 包一层，遇到 `PermissionError` 时直接把内容写到最终缓存路径。

```bash
python tools/patch_windows_triton.py --package-root D:\Works\ACE-Step-1.5-portable
```

脚本是幂等的，重复执行不会出问题；目录里如果已经有别人的 `sitecustomize.py`，会先备份再以标记块的形式追加，不动原有内容。

验证：

```bash
"D:\Works\ACE-Step-1.5-portable\python_embeded\python.exe" -c "
from triton.runtime import cache
print('补丁已生效:', getattr(cache.FileCacheManager.put, '_sfs_patched', False))
import diffusers; print('diffusers', diffusers.__version__)
"
```

```text
补丁已生效: True
diffusers 0.36.0
```

想撤销：`python tools/patch_windows_triton.py --uninstall --package-root <目录>`。

细节与排查过程见 [docs/01-Windows部署实录.md](docs/01-Windows部署实录.md)。

---

## 命令

| 命令 | 做什么 |
|---|---|
| `sfs doctor` | 环境自检：服务、显卡、便携包、自带 Python、补丁、模型、磁盘 |
| `sfs check <歌词文件>` | 歌词结构体检，提交前跑 |
| `sfs styles` | 列出内置风格模板 |
| `sfs style <键名>` | 看某个模板的完整参数 |
| `sfs make` | 出歌 |
| `sfs songs` | 看已经出过哪些歌 |

### sfs make 的常用参数

```bash
sfs make \
  --style folk \                  # 内置模板；或用 --caption 直接写风格描述
  --lyrics-file lyrics.txt \      # 也可以 --lyrics "直接给文本"
  --duration 120 \                # 秒。上游支持 10 到 600
  --out 给妈妈的歌.mp3 \
  --seed 42 \                     # 固定种子可以复现同一首
  --batch 3 \                     # 一次出三版，挑一个
  --instrumental                  # 纯音乐，忽略歌词
```

- `--caption` 优先于 `--style`。风格描述建议按「乐器 + 人声 + 情绪 + 速度 + 副歌走向」来写
- `--dry-run` 只体检不生成，会把将要提交的请求打出来
- 每次生成都会在音频旁写一个同名 `.json`，记下完整请求参数和模型补出来的元数据，方便复现

---

## 歌词怎么写

上游只给两条硬约束：**总长不超过 4096 字符**、**建议用结构标签**。其余的坑得自己踩。

`sfs check` 会把问题分三级报出来：

```text
歌词体检
==============================================
段落 5 ｜ 行数 23 ｜ 字符 209/4096 ｜ 汉字占比 81%

结构：
  · [0] Verse 1      主歌     4 行  最宽 22 列
  · [1] Chorus       副歌     4 行  最宽 23 列
  · [2] Verse 2      主歌     4 行  最宽 16 列
  · [3] Chorus       副歌     4 行  最宽 23 列
  · [4] Outro        尾奏     2 行  最宽 16 列

没有发现问题。可以出歌了。
```

- **错误**：踩了硬限制，不改就出问题
- **警告**：会明显影响成品质量的经验阈值
- **建议**：可以更好的地方

其中行宽判据按**显示宽度**算（一个汉字算 2 列，一个 ASCII 字符算 1 列）。经验上单行超过 40 列就偏长，唱起来换不过气；超过 64 列基本可以确定唱不顺。这是统计出来的常见区间，不是判卷标准，你的歌你说了算。

---

## 实测数据

一台 RTX 4080 16GB 上的数字，详细拆分见 [docs/02-性能实测-RTX4080.md](docs/02-性能实测-RTX4080.md)。

| 项 | 实测值 |
|---|---|
| 冷启动（含把 9.4GB 模型载入显存） | 73 秒 |
| 提交到出歌 | 10 秒纯器乐 15.8 秒；120 秒人声 30.7 秒；之后每首 10 到 15 秒 |
| 音频时长 | 120 秒（上游上限是 600 秒） |
| 规格 | 48 kHz 立体声，MP3 约 2 MB |
| 模型 | DiT `acestep-v15-turbo`（8 步）+ LM `acestep-5Hz-lm-1.7B` |
| 显存 | 约 14.2 GB / 16 GB（DiT 与 LM 同时在卡上） |
| 一次性部署 | 便携包下载 3m54s + 解压 20m6s + 首次下模型约 15min |

**「第一首特别慢」这个坑值得单说。** 上游默认是懒加载（`ACESTEP_NO_INIT` 默认
`true`，见上游 `acestep/api/startup_model_init.py`）—— 引擎十几秒就报「就绪」，
但其实一个模型都没载入，真正的载入被推迟到第一次出歌。实测那一次要等 **72 秒**
（10 秒纯器乐）到 **172 秒**（90 秒人声），而这段等待发生在界面上，用户只会以为
卡住了。

本项目的启动脚本反过来做：设 `ACESTEP_NO_INIT=false`，让引擎**启动时**就把模型
载进显存。等待总量差不多，但它被挪到了有进度提示的那一步 —— 界面上点出歌，
15 到 30 秒就出来。

模型会自己补全元数据，不用你指定。同一段风格描述两次跑出来的结果不一样：

| 风格描述 | 模型自判 |
|---|---|
| 温暖的中文流行民谣，木吉他为主，钢琴点缀，女声 | 79 BPM / G major |
| 同上，重跑一次 | 77 BPM / D major |
| 安静的钢琴叙事曲，独奏钢琴为主，弦乐轻铺底，女声 | 71 BPM / F major |

**确定性不等于稳定性。** 固定 `--seed` 可以让同一首完全复现，但换一个词的措辞，BPM 和调式就可能全变。

---

## 目录结构

```text
song-for-someone/
├── song_for_someone/
│   ├── __init__.py
│   ├── __main__.py          python -m song_for_someone 入口
│   ├── client.py            ACE-Step HTTP 客户端
│   ├── cli.py               命令行入口
│   ├── common.py            命令行与网页共用的工具函数
│   ├── lyrics.py            歌词解析与结构体检
│   ├── styles.py            风格模板
│   ├── doctor.py            环境自检 ＋ 便携包探测（找 Python、site-packages）
│   ├── engine.py            拉起上游引擎（起服务、等就绪、停服务）
│   ├── web.py               本地网页服务（标准库 http.server）
│   └── webapp/              网页界面静态资源（手写，无构建）
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── favicon.svg
├── 启动.bat                 Windows 一键启动（双击）
├── start.py                 跨平台一键启动入口
├── tools/
│   └── patch_windows_triton.py    Windows triton 补丁
├── examples/                可直接跑的示例
├── docs/                    部署实录、实测数据、常见问题、图形界面说明
│   └── design/              PRD 与架构设计文档
├── tests/                   276 个单元测试，零依赖
├── screenshots/             界面截图（公众号文章用）
├── .github/workflows/       CI：跑测试
├── CHANGELOG.md
├── CONTRIBUTING.md
├── songs/                   出歌的输出目录（默认，不进版本库）
└── pyproject.toml
```

跑测试：

```bash
python -m unittest discover -s tests -t .
```

---

## 常见问题

**服务连不上？**
先 `sfs doctor`。多半是 `start_api_server.bat` 没起来，或者端口不是 8001，用 `--base-url` 指一下。

**出歌很慢？**
模型首次加载要 1 到 2 分钟（planner 初始化实测 138 秒），之后就快了。整个过程模型常驻显存，别中途关服务。

**能不能不要人声？**
加 `--instrumental`，或者歌词直接写 `[Instrumental]`。

**能不能指定 BPM 和调式？**
可以，`--bpm 79 --key "G major"`。不给就让模型自己判。

**歌能商用吗？**
模型权重是 MIT 许可，可以商用。但**歌词是你写的，版权归你；成品怎么用，你自己判断**。这个项目不提供法律意见。

**数据会不会传出去？**
不会。整条链路是本机的，客户端只连 `127.0.0.1`。

---

## 上游与致谢

真正的功臣是 [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)（StepFun 与 ACE Studio），MIT 许可。这个项目只是它的外围工具，**不含任何模型权重或上游代码**。

接口调用的用法参考了上游的 [API 文档](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/API.md)与 [推理文档](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INFERENCE.md)。字段以源码 `acestep/api_server.py` 为准。

## 许可

MIT，见 [LICENSE](LICENSE)。
