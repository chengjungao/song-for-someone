# 更新日志

本项目的重要变更都记在这里。版本号遵循语义化版本。

## 0.2.0

加了「普通人也能用」的图形界面。命令行（`sfs`）保持原样，一行没变。

新增：

- **本地网页界面**（`song_for_someone/web.py`，纯标准库 `http.server`）：
  风格下拉、歌词实时体检、诚实进度条、试听与下载、我的作品。只监听
  `127.0.0.1`，不联网、不上传。
- **一键启动**：Windows 双击 `启动.bat`；跨平台执行 `python start.py`。
  就绪后自动打开浏览器；端口被占用时自动顺延。
- **界面静态资源**（`song_for_someone/webapp/`）：手写 HTML/CSS/JS，
  **无构建步骤、无前端框架**。
- **公共模块**（`song_for_someone/common.py`）：把 `human_duration` /
  `default_out_name` / `write_sidecar` 从 `cli.py` 抽出，供命令行与网页共用；
  新增 `sanitize_filename_part` 做文件名消毒。`cli.py` 对外行为逐字不变。
- **界面使用说明**（`docs/04-图形界面使用.md`）。
- **Web 层测试**（`tests/test_web.py`）：临时端口 + 假客户端 + 临时目录，
  不依赖 GPU 与真实服务。

变化：

- 版本号 `0.1.0` → `0.2.0`（`__init__.py` 与 `pyproject.toml`）。
- `pyproject.toml` 的 `package-data` 增加 `webapp/*`，让静态资源随包分发。
- **`dependencies` 仍然为空** —— 零第三方依赖的承诺不变。

没有变化（有意为之）：

- `dependencies = []`。
- 6 个 CLI 子命令（`doctor` / `check` / `styles` / `style` / `make` / `songs`）
  的行为与输出，除 `--version` 显示 `0.2.0` 外一律不变。
