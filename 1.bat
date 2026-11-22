@echo off
chcp 65001 >nul
cls
echo === 代理节点管理控制台 - 运行主程序（开发态） ===
python Proxy_Manager_Studio.py
if errorlevel 1 pause
