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

## 许可

MIT，见 `LICENSE`。提交即表示你同意按此许可分发你的贡献。
