@echo off
title 创意面板
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  本文件是 GBK(cp936) 存的，因为本机控制台代码页就是 GBK。
rem  千万别改成 UTF-8，也别加 chcp 65001 —— 实测那样 cmd 会在读到
rem  文件中途换代码页、多字节行错位，报 'xxx' is not recognized。
rem  行尾必须是 CRLF（仓库 .gitattributes 也这么钉死）。
rem  解释器写死绝对路径：本机 PATH 里的 python 不是它。
rem ---------------------------------------------------------------------

set "PYTHON=C:\ProgramData\anaconda3\python.exe"
set "URL=http://127.0.0.1:8472"

if not exist "%PYTHON%" (
    echo [错误] 找不到 Python：%PYTHON%
    echo.
    echo     本机装了不止一个 Python，PATH 里那个不一定是它。
    echo     请改本文件里 PYTHON 后面那个路径，指向实际解释器。
    echo.
    pause
    exit /b 1
)

rem 面板已经在跑就别再起一个：双击第二次会撞端口，报错栈对用户没意义。
rem 判活打的是 /api/health，不是"端口通不通"——端口被别的程序占着同样
rem 起不来，但那种情况应该让 uvicorn 自己报 address already in use，
rem 别在这里把两件事混成一句。
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2).StatusCode -eq 200) { exit 0 } } catch { } ; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo 创意面板已经在运行，直接打开浏览器即可。
    start "" "%URL%"
    echo.
    pause
    exit /b 0
)

echo ============================================================
echo   创意面板
echo ============================================================
echo.
echo   面板地址:  %URL%
echo   数据库:    data\panel.db
echo   停止服务:  在这个窗口按 Ctrl+C
echo.
echo   浏览器会自动打开。没打开就手动访问上面那行地址。
echo ============================================================
echo.

rem uvicorn 要一两秒才监听，立刻开浏览器会看到"无法访问此页面"。
rem 用 ping 计时而不是 timeout：timeout 需要控制台输入句柄，
rem 被别的程序拉起时会直接失败，ping 不会。
start "" /min cmd /c "ping -n 5 127.0.0.1 >nul & start %URL%/"

"%PYTHON%" -m uvicorn server.app:app --host 127.0.0.1 --port 8472
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
    echo [服务已退出] 退出码 %CODE%，原因在上面几行。
    echo.
    echo     常见两种：
    echo       1. 端口 8472 被别的程序占了 —— 关掉那个程序再双击本文件
    echo       2. 缺依赖 —— 跑一次：%PYTHON% -m pip install fastapi uvicorn httpx
) else (
    echo 服务已正常停止。
)
echo.
echo 把上面的输出整段留给 agent，定位问题最快。
echo.
pause
