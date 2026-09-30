# -*- coding: utf-8 -*-
"""测试包。

有这个文件，``python -m unittest discover -s tests`` 才能把 tests 目录当成
可导入的包来发现用例。此外它还负责一件事：**把测试进程的输出编码固定成
UTF-8**，原因见下。
"""

# 测试进程也要做真实入口做的那一步准备。
#
# 真实运行时，start.py / cli.py / web.py 的 main() 第一步就是把 stdout 与
# stderr 固定成 UTF-8（见 ``song_for_someone.common.setup_console``）。但测试
# 常常直接调内部函数，绕过了入口那一步。于是在英文 Windows（cp1252）或中文
# Windows（cp936）上，产品代码里任何一句中文提示都会让 ``print`` 抛
# UnicodeEncodeError —— 测试红了，可产品其实是好的。
#
# 这个坑的症状特别迷惑人：挂掉的总是**排在最前面的那几个**测试，后面的一律
# 正常。因为只要有一个测试跑了 ``main()``，stdout 就被永久改成 UTF-8，后面的
# 测试跟着沾光。于是「哪几条挂」取决于字母序，改个测试名都可能换一批。
#
# 在包导入时修一次。需要验证真实编码行为的测试不受影响 —— 它们自己起子进程、
# 自己控制环境（见 ``test_web_qa.TestConsoleEncoding``）。
from song_for_someone.common import setup_console

setup_console()
