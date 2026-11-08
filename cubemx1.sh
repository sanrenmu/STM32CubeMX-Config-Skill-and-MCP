#!/bin/sh
# cubemx1 启动器（Linux / macOS / Git Bash）：在任意目录都能用，不需要 cd 到 mcp/。
#
# 原理：把本文件所在的 mcp/ 目录塞进 PYTHONPATH，再跑 python -m cubemx1。
# 这样"解压即用"，不用 pip 安装。
#
# 用法：
#   <包根>/cubemx1.sh version
#   <包根>/cubemx1.sh --json peripherals describe SPI1
# 想更省事就 chmod +x 并放进 PATH，之后直接敲 cubemx1。
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHONPATH="$HERE/mcp${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 挑一个**真能跑**的解释器。
# 注意：Windows 上 `python3` 可能是个"应用商店占位程序"，`command -v` 找得到、
# 一跑就退出（码 49）——所以不能只看"存不存在"，要真跑一下验证。
PY=""
for cand in "$PYTHON" python3 python; do
    [ -n "$cand" ] || continue
    if "$cand" -c "import sys" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "cubemx1: 找不到可用的 Python（试过 python3 / python）。装了 Python 3.8+ 就行。" >&2
    exit 127
fi
# `-B`：不写 `__pycache__`——解压即用的包不该在被跑过一次之后多出一堆缓存文件
# （`.cmd` 那份一直带着 `-B`，两份启动器要一致）。
#
# ⚠ 姊妹文件 `cubemx1.cmd` 的注释**故意是英文**，不是漏翻：
# cmd.exe 按 **OEM 代码页**读批处理文件，UTF-8 的中文注释会被误码、**当成命令执行**
# （实测：启动器直接报 "'…' 不是内部或外部命令"）。所以那份**注释必须 ASCII**，
# 中文说明就放在这里（和仓库里那套开发文档里）。
exec "$PY" -B -m cubemx1 "$@"
