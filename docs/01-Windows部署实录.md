# Windows 部署实录

这一篇记录在一台 Windows 11 机器上，从零把 ACE-Step 1.5 跑起来、修掉 triton 崩溃、
成功出一首歌的完整过程。所有数字都是实测值。

机器：Windows 11（10.0.26200）｜ RTX 4080 16GB ｜ 驱动 616.92

---

## 一、时间账

| 环节 | 耗时 | 说明 |
|---|---|---|
| 下载官方便携包 | 3 分 54 秒 | 2.24 GB，`ACE-Step-1.5.7z` |
| 解压 | **20 分 6 秒** | 57,494 个文件，含 `python_embeded` 与 `PortableGit` |
| 修复 triton 崩溃 | 见第二节 | 这一步不做，服务根本起不来 |
| 首次启动下载模型 | 约 15 分钟 | 9.4 GB，走 ModelScope 源 |
| Planner（5Hz LM）初始化 | **138 秒** | 每次冷启动都要等 |
| 之后每次生成 | 10 到 30 秒 | 模型常驻显存，不重启就一直快 |

解压那 20 分钟是最容易被低估的一步。七万个小文件在 NTFS 上写入，杀软还会逐个扫描。
建议把便携包放在 SSD 上，并且把目录加进杀软白名单。

---

## 二、triton 3.3.1 在 Windows 上必崩

这是本次部署唯一的硬阻塞，也是最值得写下来的一段。

### 症状

`start_api_server.bat` 启动后，服务在初始化阶段直接退出。日志里能看到：

```text
PermissionError: [WinError 5] Access is denied:
'...\triton\cache\...\cuda_utils.pyd'
```

随后是 diffusers 的导入失败。整个服务起不来。

### 根因

问题在 `triton/runtime/cache.py` 的 `FileCacheManager.put()`。它的逻辑大致是：

1. 把编译产物写到一个临时文件
2. 用 `os.replace(temp_path, final_path)` 原子地换到最终位置
3. 如果第 2 步失败，兜底只 try 了 `os.remove(temp_path)`

在 Windows 上，第 2 步会因为**目标文件被占用**而失败。而这里的目标正是刚编译出来的
`cuda_utils.pyd`，**它已经被进程加载进内存，文件句柄锁着**。于是第 3 步的
`os.remove(temp_path)` 换成同一个 `WinError 5`，异常直接冒出 `put()`。

这个异常一路顶到调用方，也就是 diffusers 的导入链。导入失败，服务自然起不来。

### 为什么它会自我维持

这一层比崩溃本身更麻烦：

缓存文件永远写不进去，所以下次启动时**缓存里什么都没有**，于是重新编译一遍，
于是在同一个地方再失败一次。每次启动都从零开始，每次都卡死。

### 排除过的方案

| 尝试 | 结果 |
|---|---|
| 换 `TRITON_CACHE_DIR` 到别的盘 | **无效**。只是换个地方写，锁还在 |
| 脱离沙箱／关掉限制 | **无效**。这是真实的 Windows 文件锁语义 |
| 检查磁盘权限、目录 ACL | 正常。不是权限配置问题 |
| 提权运行 | 无效。文件被同进程持有，跟用户权限无关 |

确认是真实文件锁而不是环境策略，这一点很关键，否则会一直在权限配置上打转。

### 补丁

在 site-packages 下放一个 `sitecustomize.py`。Python 启动时会自动 import 这个模块，
于是补丁可以在任何业务代码之前生效。

补丁做的事很简单：把 `FileCacheManager.put` 包一层，遇到 `PermissionError` 时不再往上抛，
而是**直接把内容写到最终缓存路径**，然后返回路径。

为什么这样是对的：

- 缓存内容本身是正确的，只是绕过了那个会失败的原子替换动作
- 下一次启动就能命中缓存，不再重复编译
- 非 Windows 平台上原样抛出异常，不掩盖真实问题
- 只替换一个方法，不修改 triton 的源码文件

安装：

```bash
python tools/patch_windows_triton.py --package-root D:\Works\ACE-Step-1.5-portable
```

脚本会把补丁以标记块的形式写进去，幂等，可卸载：

```text
# --- BEGIN song-for-someone:triton-patch ---
...
# --- END song-for-someone:triton-patch ---
```

### 验证

光看文件存不存在不够。要确认补丁**真的挂到了运行时对象上**：

```bash
"D:\Works\ACE-Step-1.5-portable\python_embeded\python.exe" -c "
import sys
print('sitecustomize 已加载:', 'sitecustomize' in sys.modules)
from triton.runtime import cache
print('put 已被替换:', getattr(cache.FileCacheManager.put, '_sfs_patched', False))
import diffusers
print('diffusers:', diffusers.__version__)
import torch
print('CUDA:', torch.cuda.is_available())
"
```

实测输出：

```text
sitecustomize 已加载: True
put 已被替换: True
diffusers: 0.36.0
torch: 2.7.1+cu128 | CUDA: True
```

这四条全过，才算真的修好了。

---

## 三、环境事实清单

部署完之后按下的实际版本，供对照：

| 组件 | 版本 |
|---|---|
| 便携包内 Python | 3.11.9 |
| triton | 3.3.1 |
| torch | 2.7.1+cu128（CUDA 12.8） |
| diffusers | 0.36.0 |
| API 服务端口 | 8001（`start_api_server.bat` 里的 `set PORT=8001`） |

模型权重落在 `<便携包>/checkpoints/`，逐目录实测大小：

| 目录 | 用途 | 大小 |
|---|---|---|
| `acestep-v15-turbo` | DiT，实际生成音频 | 4.46 GB |
| `acestep-5Hz-lm-1.7B` | LM planner，补全元数据与曲式 | 3.50 GB |
| `Qwen3-Embedding-0.6B` | 文本编码 | 1.12 GB |
| `vae` | 音频编解码 | 0.31 GB |
| **合计** | | **9.40 GB** |

---

## 四、其他几个坑

**控制台编码。** Windows 的 cmd 与 PowerShell 默认是 GBK 代码页，直接 print 中文会乱码，
严重时抛 `UnicodeEncodeError` 把程序打断。本项目在 CLI 入口把标准输出切成 UTF-8，
并且对无法编码的字符做降级替换，保证不会因为一个字符崩掉整次生成。

**中文文件名。** 输出文件名建议用英文，或者明确用 UTF-8 处理。本项目统一用 `pathlib`
读写，文件名走 UTF-8 编码。

**模型下载源。** 国内直连 HuggingFace 通常不通。ACE-Step 的模型在 ModelScope 上有镜像，
便携包会优先走通的那条路。

**显存。** DiT 与 LM 会同时在卡上，实测 14.2 GB / 16 GB。如果同时开着别的推理服务，
很容易 OOM。`sfs doctor` 会检查占用情况。
