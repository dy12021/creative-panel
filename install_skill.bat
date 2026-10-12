@echo off
title 安装 creative-panel skill
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  本文件是 GBK(cp936) 存的，因为本机控制台代码页就是 GBK。
rem  千万别改成 UTF-8，也别加 chcp 65001（原因见 start.bat 里的说明）。
rem  行尾必须是 CRLF（仓库 .gitattributes 也这么钉死）。
rem
rem  同步方向是单向的：以项目里的 skill\ 为准，覆盖安装目录。
rem  安装目录是产物，不要直接改那里的文件——下次同步就被覆盖了。
rem ---------------------------------------------------------------------

rem  SPEC 第 10 节：bat 不展开 ~，这里必须写 %USERPROFILE%。
set "SRC=%~dp0skill"
set "DST=%USERPROFILE%\.minimax\skills\creative-panel"

if not exist "%SRC%\panel.py" (
    echo [错误] 找不到 %SRC%\panel.py
    echo.
    echo     本脚本要放在项目根目录，和 skill\ 并排。
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   同步 creative-panel skill
echo ============================================================
echo.
echo   从:  %SRC%
echo   到:  %DST%
echo.

if not exist "%DST%" (
    mkdir "%DST%"
    if errorlevel 1 (
        echo [错误] 无法创建 %DST%
        echo.
        pause
        exit /b 1
    )
)

rem 不用 /Q：让 xcopy 把每个文件打出来，用户能当场核对同步了什么。
rem
rem 不用 /XD 排除 __pycache__：本机 xcopy（Win10 26300.9550）任何排除型
rem 开关都是坏的——/XD、/XF，放前放后、参数给目录名或绝对路径，都报
rem "Invalid number of parameters"，退出码 4，且复制 0 个文件。
rem 实测 8 种组合只有 /E /I /Y 是好的。别把它加回来。
rem 目标路径不带尾反斜杠：目录已由上面的 mkdir 建好，多一个反斜杠没好处。
xcopy "%SRC%\*" "%DST%" /E /I /Y
if errorlevel 1 (
    echo.
    echo [错误] 复制失败，原因在上面 xcopy 的输出里。
    echo.
    pause
    exit /b 1
)

echo.
echo   装到了这里，请核对：
echo.
for %%f in ("%DST%\*") do echo     %%~nxf   %%~zf 字节
echo.
echo   新开一轮对话，skill 才会被加载。
echo.
echo   注意：那是安装产物。改 skill 请改项目里的 skill\ 再重跑本脚本，
echo         直接改安装目录里的文件，下次同步就被覆盖了。
echo.
pause
