# -*- coding: utf-8 -*-
"""回环地址感知的 HTTP 请求工具。

本项目的每一处出站请求都指向**用户自己电脑上**的 ACE-Step 服务
（默认 ``http://127.0.0.1:8001``）。Python 的 ``urllib`` 会读取环境变量
``http_proxy`` / ``https_proxy`` / ``no_proxy``，但**不会**读取 Windows 系统
（IE/设置）里的代理配置。

因此，装了代理工具（Clash / v2ray 等）并**手动设过** ``http_proxy`` 环境变量的
用户——恰恰就是「自己装 ACE-Step、跑本地模型、为 pip/git/HuggingFace 配代理」
的这批核心用户——如果代理没正确绕过 ``127.0.0.1``，请求就会被转发到代理并失败，
界面只会显示「连不上服务」。这个症状会把用户引向完全错误的排查方向（反复查
ACE-Step 启没启、端口对不对、模型下完没），而真因藏在环境变量里，界面上任何
提示都指不到那里，是最难自诊断的一类故障。

对策：**只对回环地址**（``127.0.0.0/8``、``localhost``、``::1``）强制绕过代理，
**非回环地址（局域网、公网）的行为一字不改**。

依赖铁律：本模块**只依赖标准库**，不 import 包内任何其它模块 —— 这样
``client`` / ``web`` 都能安全引用它，不会产生循环依赖。
"""

from __future__ import annotations

import shutil
import urllib.parse
import urllib.request
from typing import Any, IO, Optional, Tuple

# 精确匹配的回环主机名（比较前统一小写）。IPv6 回环 ``::1`` 经 urlsplit
# 还原出来是不带方括号的形式，这里也把带方括号的写法一并认下。
_LOOPBACK_HOSTS = frozenset({"localhost", "::1", "[::1]"})

# 一个「对谁都不过代理」的 opener：``ProxyHandler({})`` 的空字典会让它完全
# 忽略环境变量里的代理配置。构造一次复用，避免每次请求都重建。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 用于区分「调用方没传 timeout」与「调用方显式传了 None」。
_UNSET = object()


def is_loopback_host(host: Optional[str]) -> bool:
    """判断主机名/地址是否是本机回环地址。

    :param host: 主机名或 IP（可带方括号）。``None`` / 空串返回 ``False``。
    :return: 是回环地址返回 ``True``。
    """
    if not host:
        return False
    candidate = str(host).strip().lower()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate in _LOOPBACK_HOSTS:
        return True
    # 整个 127.0.0.0/8 网段都是回环。
    return candidate.startswith("127.")


def is_loopback_url(url: Any) -> bool:
    """判断 URL 是否指向本机回环地址。

    :param url: 可以是字符串，也可以是 ``urllib.request.Request``（取其
        ``full_url``）。
    :return: 指向回环地址返回 ``True``；解析不出主机名的返回 ``False``。
    """
    target = getattr(url, "full_url", None)
    if target is None and isinstance(url, str):
        target = url
    if not target:
        return False
    try:
        host = urllib.parse.urlsplit(target).hostname
    except ValueError:
        return False
    return is_loopback_host(host)


def urlopen(url: Any, data: Optional[bytes] = None, timeout: Any = _UNSET) -> Any:
    """``urllib.request.urlopen``，但对回环地址强制绕过代理。

    非回环地址完全走 ``urllib.request.urlopen`` 的原路径，行为不变。

    :param url: URL 字符串或 ``Request`` 对象。
    :param data: POST 数据（同 urllib）。
    :param timeout: 超时秒数；不传则沿用 urllib 的默认行为。
    :return: 可作上下文管理器使用的响应对象。
    """
    if is_loopback_url(url):
        if timeout is _UNSET:
            return _NO_PROXY_OPENER.open(url, data=data)
        return _NO_PROXY_OPENER.open(url, data=data, timeout=timeout)
    if timeout is _UNSET:
        return urllib.request.urlopen(url, data=data)
    return urllib.request.urlopen(url, data=data, timeout=timeout)


def urlretrieve(url: str, filename: Any) -> Tuple[str, Any]:
    """``urllib.request.urlretrieve``，但对回环地址强制绕过代理。

    :param url: 目标 URL。
    :param filename: 本地落盘路径。
    :return: ``(filename, headers)``，与 ``urllib.request.urlretrieve`` 一致。
    """
    if is_loopback_url(url):
        handle: IO[bytes]
        with _NO_PROXY_OPENER.open(url) as response:
            headers = response.headers
            with open(filename, "wb") as handle:
                shutil.copyfileobj(response, handle)
        return str(filename), headers
    return urllib.request.urlretrieve(url, filename)


__all__ = [
    "is_loopback_host",
    "is_loopback_url",
    "urlopen",
    "urlretrieve",
]
