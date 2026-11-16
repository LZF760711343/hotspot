# -*- coding: utf-8 -*-
"""打包脚本：生成单文件 exe"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

cmd = [
    PY, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile",                       # 单文件
    "--windowed",                      # 无控制台黑窗口（ notebook 服务后台运行）
    "--name", "热点收集助手",
    "--add-data", os.path.join(HERE, "templates") + os.pathsep + "templates",
    "--hidden-import", "flask",
    "--hidden-import", "jinja2",
    "--hidden-import", "werkzeug",
    "--hidden-import", "urllib.request",
    os.path.join(HERE, "app.py"),
]
print(" ".join(cmd))
subprocess.check_call(cmd, cwd=HERE)
print("\n打包完成，exe 位于 dist/热点收集助手.exe")
