@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  song-for-someone 一键启动（Windows）
rem  双击本文件即可。不需要输入任何命令。
rem ============================================================

where python >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0start.py"
goto done

:no_python
echo.
echo  没有找到 Python。这个程序需要你先装一个 Python（免费）。
echo.
echo  怎么装：
echo    1. 打开  https://www.python.org/downloads/
echo    2. 点网页上那个黄色的 Download 按钮，下载后双击安装
echo    3. 安装的第一屏，务必勾选最下面的 "Add Python to PATH"
echo    4. 装好后把这个窗口关掉，再重新双击本文件
echo.
goto done

:done
echo.
pause
endlocal
