@echo off
chcp 65001 >nul
cls
echo === 调试运行（输出写入 debug.txt） ===
python -X faulthandler Proxy_Manager_Studio.py > debug.txt 2>&1
type debug.txt
pause
