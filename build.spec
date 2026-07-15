# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

# 自动发现所有子模块
_hidden = []
for pkg, dirs in [("tools", "src/tools"), ("plugins", "src/plugins"),
                  ("team", "src/team"), ("hooks", "src/hooks"),
                  ("mcps", "src/mcps"), ("skills", "src/skills"),
                  ("memory", "src/memory"), ("learning", "src/learning"),
                  ("web", "src/web"), ("utils", "src/utils"),
                  ("autonomous", "src/autonomous")]:
    for f in Path(dirs).glob("*.py"):
        if f.stem != "__init__":
            _hidden.append(f"{pkg}.{f.stem}")

# 插件子目录
for d in Path("src/plugins").iterdir():
    if d.is_dir() and (d / "__init__.py").exists():
        _hidden.append(f"plugins.{d.name}")

block_cipher = None

a = Analysis(
    ['src/main.py'],
    datas=[
        ("config/", "config"),
    ],
    hiddenimports=_hidden,
    excludes=[
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'tensorflow', 'torch',
        'notebook', 'ipython', 'jupyter', 'boto3',
    ],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    name='agent',
    console=True,
    upx=True,
)
