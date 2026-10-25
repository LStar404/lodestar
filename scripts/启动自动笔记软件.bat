@echo off
setlocal EnableExtensions
title note-gen 自动笔记软件
rem ============================================================
rem  note-gen 一键启动器
rem  用法：
rem    启动自动笔记软件.bat               启动服务并打开浏览器
rem    启动自动笔记软件.bat --no-browser   只启动服务（不打开浏览器）
rem    启动自动笔记软件.bat 9000           指定端口启动
rem ============================================================

set "APP_DIR=%~dp0"
if not exist "%APP_DIR%cli.py" set "APP_DIR=%~dp0note-gen\"
set "PORT=8787"
set "NO_BROWSER=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-browser" goto set_nobrowser
if /i "%~1"=="-n" goto set_nobrowser
set "PORT=%~1"
shift /1
goto parse_args

:set_nobrowser
set "NO_BROWSER=1"
shift /1
goto parse_args

:args_done
if not exist "%APP_DIR%cli.py" goto err_noapp
where python >nul 2>nul
if errorlevel 1 goto err_nopython

cd /d "%APP_DIR%"

rem ---------- 1) 依赖检查 ----------
python -c "import fastapi, uvicorn, pydantic, yaml, filelock, httpx, pypdf, jinja2, multipart" >nul 2>nul
if errorlevel 1 goto install_deps
goto deps_ok

:install_deps
echo [提示] 检测到缺少依赖，正在安装（首次较慢）...
python -m pip install -r requirements.txt
if errorlevel 1 python -m pip install fastapi uvicorn pydantic PyYAML filelock httpx pypdf jinja2 json-repair pylatexenc sqlite-vec genanki python-multipart

:deps_ok
rem ---------- 2) 前端构建检查 ----------
if exist "viewer\dist\index.html" goto frontend_ok
echo [提示] 前端未构建，尝试构建 viewer...
where pnpm >nul 2>nul
if errorlevel 1 goto frontend_warn
pushd viewer
call pnpm install --registry https://registry.npmmirror.com
call pnpm run build
popd
:frontend_warn
if not exist "viewer\dist\index.html" echo [警告] 前端未构建，网页将显示占位页；可手动执行 cd viewer 然后 pnpm install 与 pnpm run build
:frontend_ok

rem ---------- 3) 启动服务 + 打开浏览器 ----------
echo.
echo ============================================================
echo   note-gen 自动笔记软件
echo   地址 : http://127.0.0.1:%PORT%
echo   目录 : %APP_DIR%
echo   停止 : 在本窗口按 Ctrl+C 或直接关闭窗口
echo ============================================================
echo.

if "%NO_BROWSER%"=="1" goto start_server
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%PORT%"

:start_server
python cli.py serve --port %PORT%
echo.
echo [完成] 服务已停止。
pause
exit /b 0

:err_noapp
echo [错误] 未找到应用目录：%APP_DIR%
echo        请把本脚本放在 note-gen 同级目录或 note-gen 内部。
pause
exit /b 1

:err_nopython
echo [错误] 未找到 python，请先安装 Python 3.11 及以上版本并加入 PATH。
pause
exit /b 1
