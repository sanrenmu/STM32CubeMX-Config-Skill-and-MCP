@echo off
rem cubemx1 launcher (Windows): run from any directory, no need to cd into mcp/.
rem
rem How: put this file's mcp/ directory on PYTHONPATH, then run `python -m cubemx1`.
rem That makes the package "unzip and run" — no pip install.
rem
rem Usage:
rem   <package root>\cubemx1.cmd version
rem   <package root>\cubemx1.cmd --json peripherals describe SPI1
rem Add the package root to PATH to just type `cubemx1`.
rem
rem NOTE: the comments in this file are English ON PURPOSE, not an oversight.
rem cmd.exe reads batch files in the OEM code page; UTF-8 Chinese comments get
rem mis-decoded and executed as commands (measured: the launcher fails with
rem "'...' is not recognized as an internal or external command"). Keep them
rem ASCII. The Chinese notes live in cubemx1.sh and in the dev handbook.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%mcp;%PYTHONPATH%"
rem -B: do not write __pycache__ — an unzip-and-run package should not grow
rem cache files after the first run (cubemx1.sh carries -B too; keep both alike).
rem -X utf8: on Windows Python encodes stdout with the system code page by
rem default, which garbles Chinese; pin it to UTF-8.
python -X utf8 -B -m cubemx1 %*
exit /b %ERRORLEVEL%
