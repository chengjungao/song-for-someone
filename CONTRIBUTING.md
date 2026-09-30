# 参与贡献

谢谢你有兴趣改进 `song-for-someone`。这个项目的定位是「普通读者拿过去就能用」，
所以有两条硬约束，改代码前请先读一下。

## 三条不可打破的约定

1. **零第三方依赖**。`pyproject.toml` 里的 `dependencies` 必须一直是空列表。
   所有实现只用 Python 标准库 —— 这样读者 `git clone` 下来就能跑，不用先建
   虚拟环境再装包。新增代码不许 `import` 任何第三方包。

2. **界面不许有构建步骤**。网页界面（`song_for_someone/webapp/`）是手写的
   HTML / CSS / JS，不引入 React / Vue / npm / vite，也没有打包环节。

3. **命令行行为不变**。`sfs` 的 6 个子命令（`doctor` / `check` / `styles` /
   `style` / `make` / `songs`）的输出与退出码是对读者的承诺，改动前请想清楚。
   网页界面是**新增的一层**，不是替换。

## 怎么跑

不用安装任何东西，有个 Python 3.9+ 就行：

```bash
python -m unittest discover -s tests -t .
```

跑网页界面：

```bash
python start.py          # 或 Windows 上双击 启动.bat
```

## 提 PR 时

- 让测试全绿（现有用例只允许增加，不允许删改）。
- 新增网页相关的能力，请顺带给 `tests/test_web.py` 补一个用例 —— 用假客户端
  和临时目录，**不要**依赖真实显卡或真实 ACE-Step 服务。
- 新增引擎相关的能力，补 `tests/test_engine.py` —— 用临时目录造假便携包、用假进程
  和假服务。**测试里绝不许真去拉 ACE-Step 引擎**：一次冷启动要两三分钟，还会占满
  显存。凡是跑 `start.py` 的用例，务必带上 `--no-engine`。
- 提交信息用中文，说清「改了什么、为什么」。

## 界面文案的禁用词（含一处精确例外）

面向普通用户的界面文案里，**不许**出现这些裸术语（它们是代码里的字段名，普通人看不懂）：

```text
caption  inference_steps  batch_size  guidance_scale  task_type
key_scale  time_signature  audio_duration  vocal_language  audio_format
seed（单独出现时）  traceback  Traceback  HTTP 4  HTTP 5  Exception
%completed  完成 xx%  剩余 00:
```

自检（对 `song_for_someone/webapp/` 全目录）：

```bash
grep -rnE 'caption|inference_steps|batch_size|guidance_scale|task_type|key_scale|time_signature|audio_duration|vocal_language|audio_format|traceback|Exception' song_for_someone/webapp/
```

### 唯一例外：完成页折叠区里的「复现命令」原文

`webapp/app.js` 会把后端返回的 `reproduce`（形如
`sfs make --caption "…" --lyrics-file <歌词文件> --duration 120`）**原样**显示在
**默认收起的折叠区**里（等宽字体 + 一键复制）。这里**允许**出现 `--caption` /
`--seed` 等 **CLI 的真实参数名**，因为它是**可复制执行的命令原文**，不是面向普通用户的说明文案：

- 一旦把参数名改得「更好懂」，这条命令就复制不动了，功能等于没做；
- 它默认收起，普通用户不展开就看不到，不构成打扰；
- 它是留给「想更进一步」的读者的命令行入口——这正是本项目区别于普通工具的价值。

**例外范围精确限定在这个折叠区**（DOM 里是 `#reproduce-block`），不复盖其它任何界面文案，
也不允许把 `caption` 之类的词挪到折叠区以外的地方。

## 已知限制

这些是**有意为之**的取舍，不是待修的 bug。改之前请先理解原因。

### 1. 不做 CSRF 与 DNS-rebinding 防护

网页服务是**本地单机工具**：只监听回环地址 `127.0.0.1`（红线，禁止绑 `0.0.0.0`），
面向的是「自己电脑上的自己」这一个用户，不对外提供服务。因此：

- 不引入 CSRF token —— 没有跨站身份，没有可被冒用的凭证；
- 不校验 `Host` / `Origin` 头做 DNS-rebinding 防护 —— 攻击者要利用它得先让受害者的
  浏览器去访问一个能被解析到 `127.0.0.1` 的恶意域名，而对一个本地出歌工具而言，
  这个前提下的收益与代价不成正比。

一旦要把它改成局域网共享或对外服务，**这两条必须重新评估**。

### 2. 出站请求只对回环地址绕过代理

`urllib` 会读环境变量 `http_proxy` / `https_proxy`（**不读** Windows 系统代理设置）。
装了代理工具又手动设过这些变量的用户，若代理没绕过 `127.0.0.1`，访问本机 ACE-Step
的请求会被转发去代理并失败，界面只报「连不上服务」，极难自诊断。

所以本项目所有出站请求统一走 `song_for_someone/net.py`：**回环地址**
（`127.0.0.0/8`、`localhost`、`::1`）强制绕过代理，**非回环地址照常走系统代理配置**。
新增出站请求请用 `net.urlopen` / `net.urlretrieve`，不要直接用 `urllib.request`。

### 3. `net.urlopen` 的非回环分支会复用进程级全局 opener

非回环地址走的是 `urllib.request.urlopen` 原路径，而它会**惰性构建并缓存一个进程级
全局 opener**（`urllib.request._opener`）—— 里面的 `ProxyHandler` 在**构建那一刻**
就把代理环境变量**快照**了下来。

**对本项目无影响**：我们只访问 `127.0.0.1`，走的是强制绕过代理的分支。

但如果你要把它扩展成「访问远程模型服务」，请注意两点：

- 进程运行期间再修改代理环境变量，**不会**对已缓存的 opener 生效；
- 写测试时若临时改过 `http_proxy`，收尾**必须还原** `urllib.request._opener`，
  否则会污染同进程内后续所有 HTTP 调用（症状是一大片莫名其妙的 `连接被拒`）。
  `tests/test_net.py` 里有现成的 save / restore 写法可参考。

### 4. 引擎的生命周期跟着 `start.py`

`start.py` 拉起的 ACE-Step 服务，会在 `finally` 里被主动停掉：窗口一关，界面和
引擎一起停，约 14GB 显存随即释放。

别改成「留着引擎让下次启动更快」——`start.py` 第 2 步时实测过：父进程退出后
子进程本来就会被系统带走（引擎日志停在 `Uvicorn running` 之后，既没有报错也没有
shutdown 记录），「让它自己常驻」是靠不住的。要做就做成确定的：要么主动停
（现状），要么用 `DETACHED_PROCESS` 真正脱离，并给用户一个明确的停止入口 ——
否则会留下一个占着 14GB 显存、用户不知道怎么关的后台进程。

要让引擎长留着，现在有正路：便携包里的 `启动引擎.bat` 单独开着它，
`start.py` 检测到已有引擎会直接复用，不会重复拉起。

## 许可

MIT，见 `LICENSE`。提交即表示你同意按此许可分发你的贡献。
