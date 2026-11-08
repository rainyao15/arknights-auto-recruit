@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo ============================================
echo   明日方舟公招面板 · 打包 exe
echo   （日常不用跑这个，只有改了代码才需要）
echo ============================================
echo.

rem ---- 找 Python：先看常见安装路径，再回退 PATH，并用 -c 验证不是应用商店占位符 ----
set "PY="
for %%D in (314 313 312 311 310) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python%%D\python.exe"
    )
)
if not defined PY if exist "%ProgramFiles%\Python314\python.exe" set "PY=%ProgramFiles%\Python314\python.exe"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if defined PY (
    "%PY%" -c "import sys" >nul 2>&1 || set "PY="
)
if not defined PY (
    echo [X] 找不到可用的 Python（应用商店的占位符不算）。
    echo     装一个 Python 3.10+ 并勾选 Add to PATH，再跑这个脚本。
    pause
    exit /b 1
)
echo   Python: %PY%
"%PY%" -c "import sys;print('  版本:', sys.version.split()[0])"

set OUT=dist\明日方舟公招面板
set BAK=%TEMP%\ak_panel_config_backup.yaml

echo [1/4] 检查 PyInstaller ...
"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo       安装 PyInstaller ...
    "%PY%" -m pip install -q pyinstaller
    if errorlevel 1 (echo [X] 安装失败，先手动跑： "%PY%" -m pip install pyinstaller & pause & exit /b 1)
)

echo [2/4] 备份旧 exe 旁边的 config.yaml（有的话打包后恢复）...
if exist "%OUT%\config.yaml" copy /y "%OUT%\config.yaml" "%BAK%" >nul

echo [3/4] 打包（含 OCR 模型，约 2~5 分钟）...
taskkill /F /IM 明日方舟公招面板.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul
if exist dist_tmp rmdir /s /q dist_tmp
if exist "%OUT%" rmdir /s /q "%OUT%"
if exist "%OUT%" (
    echo [X] 删不掉旧的 "%OUT%"：面板还在运行，而且是管理员权限起的。
    echo     请在任务管理器里结束「明日方舟公招面板.exe」，或在面板里关掉窗口后重试。
    pause
    exit /b 1
)
if exist build rmdir /s /q build
"%PY%" -m PyInstaller --noconfirm --clean 公招面板.spec
if errorlevel 1 (echo [X] 打包失败，看上面的报错 & pause & exit /b 1)

echo [4/4] 收尾 ...
if exist "%BAK%" (
    copy /y "%BAK%" "%OUT%\config.yaml" >nul
    del /q "%BAK%"
    echo       已恢复你改过的 config.yaml
) else (
    echo       config.yaml / data / logs 会在第一次双击时自动生成
)

rem 快捷方式指回标准位置（不管之前临时建在哪个目录）
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $l=$ws.CreateShortcut('%~dp0明日方舟公招面板.lnk'); $l.TargetPath='%~dp0%OUT%\明日方舟公招面板.exe'; $l.WorkingDirectory='%~dp0%OUT%'; $l.IconLocation='%~dp0%OUT%\明日方舟公招面板.exe,0'; $l.Save()" >nul 2>&1

echo.
echo ============================================
echo   完成！双击这个就行：
echo   %OUT%\明日方舟公招面板.exe
echo   需要管理员时它会自己弹 UAC 重启
echo ============================================
pause
