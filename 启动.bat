@echo off
rem If the Chinese text below looks garbled, your console codepage
rem is not 936. The commands are plain ASCII, so it still works.
setlocal

rem ============================================================
rem  song-for-someone 一键启动（Windows）
rem  双击本文件即可。不需要输入任何命令。
rem
rem  它会把两件事一起做完：
rem    1. 把音乐引擎（ACE-Step 本地服务）带起来
rem    2. 打开网页界面
rem
rem  首次运行要下约 10GB 模型，可能等十几分钟；
rem  之后每次冷启动约 1 分钟。
rem
rem  关掉这个窗口，界面和音乐引擎会一起停（显存随之释放）。
rem  不想每次等，可以先用便携包里的「启动引擎.bat」把引擎单独开着，
rem  本脚本检测到引擎已在跑就会直接进界面。
rem
rem  想只开界面不起引擎，可以这样运行：
rem    start.py --no-engine
rem ============================================================

where python >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0start.py" %*
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
