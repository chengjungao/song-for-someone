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

## 许可

MIT，见 `LICENSE`。提交即表示你同意按此许可分发你的贡献。
