# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 gui.py 打成 onedir 版 exe（明日方舟公招面板.exe）。

构建：
    python -m PyInstaller --noconfirm 公招面板.spec

产物：
    dist/明日方舟公招面板/明日方舟公招面板.exe   ← 双击就能用
    dist/明日方舟公招面板/config.yaml            ← 拷一份，用户随时改
    dist/明日方舟公招面板/data/recruitment.json  ← 词条数据（离线可用）
    dist/明日方舟公招面板/_internal/...          ← 依赖（别动）

为什么是 onedir 不是 onefile：
    OCR 模型 + onnxruntime 有 300MB+，onefile 每次启动都要解压到临时目录，
    冷启动 8~15 秒；onedir 直接跑，1~2 秒出界面。
"""
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.dirname(SPEC))

datas = [
    ('assets/icon.ico', 'assets'),
    # 内置默认文件：exe 第一次启动会自动复制到 exe 旁边（paths.ensure_user_files）
    ('config.yaml', '_defaults'),
    ('data/recruitment.json', '_defaults/data'),
]
binaries = []
hiddenimports = [
    # 这些是「函数里才 import」的，静态分析扫不到，必须显式声明
    'core.pc_client', 'core.adb_client', 'core.simulator', 'core.device',
    'core.elevation', 'mss', 'yaml',
]

for pkg in ('rapidocr', 'onnxruntime'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['gui.py'],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'matplotlib', 'pandas', 'scipy',
        'IPython', 'jupyter', 'notebook', 'pytest', 'setuptools', 'pip',
        'PIL.ImageQt', 'PyQt5', 'PySide2', 'PySide6', 'wx',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='明日方舟公招面板',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # upx 压 onnxruntime 的 dll 容易把 exe 搞崩，别开
    console=False,              # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='明日方舟公招面板',
)
