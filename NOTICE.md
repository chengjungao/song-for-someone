# 第三方组件与致谢

这个项目本身只有 MIT 许可的 Python 代码，**不包含任何模型权重，也不包含上游项目的代码**。
运行时依赖下面这些组件，它们是各自作者的成果。

---

## ACE-Step 1.5

音乐生成的全部能力来自这里。

- 项目：https://github.com/ace-step/ACE-Step-1.5
- 作者：StepFun 与 ACE Studio
- 许可：MIT
- 论文：https://arxiv.org/abs/2602.00744
- 模型权重：https://huggingface.co/ACE-Step/Ace-Step1.5 ／ https://modelscope.cn/models/ACE-Step/Ace-Step1.5

本项目的 `song_for_someone/client.py` 依据它的 HTTP 接口编写，字段口径来自源码
`acestep/api_server.py` 与 `docs/en/API.md`。任务状态位（0 排队、1 成功、2 失败）
沿用上游约定。

模型权重单独下载，不受本项目的 MIT 许可约束，以 ACE-Step 的模型许可为准。

---

## Triton

- 项目：https://github.com/triton-lang/triton
- 许可：MIT

`tools/patch_windows_triton.py` 是对 triton 运行时行为的一处**猴子补丁**，
用于绕开 Windows 上 `FileCacheManager.put()` 的 `PermissionError` 问题。
补丁不修改 triton 的源码，只在运行时替换一个方法的实现，可以随时用
`--uninstall` 移除。

如果上游后续修好了这个问题，这个补丁就不再需要。

---

## PyTorch / diffusers

由 ACE-Step 的便携包自带，本项目不直接依赖。

- PyTorch：https://github.com/pytorch/pytorch （BSD 许可）
- diffusers：https://github.com/huggingface/diffusers （Apache-2.0 许可）

---

## 本项目写了什么

为了避免误会，把自己的部分列清楚：

- `song_for_someone/` 下的全部模块，只用 Python 标准库
  （含网页服务 `web.py`：基于标准库 `http.server`，无任何 Web 框架；
  以及回环地址感知的 `net.py`：对 `127.0.0.1` 绕过代理环境变量）
- `song_for_someone/webapp/` 里的网页界面（手写 HTML / CSS / JS，无前端框架、无构建）
- `tools/patch_windows_triton.py` 补丁安装器
- `docs/` 里的实测数据与排查记录
- `tests/` 里的单元测试

没有复制上游代码。客户端是按公开的接口约定重新实现的。
