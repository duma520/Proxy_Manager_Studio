@echo off
chcp 65001 >nul
cls
echo === 代理节点管理控制台 - Nuitka 打包（PySide6 + clang，Windows） ===
echo 入口：Proxy_Manager_Studio.py（启动器）  +  pms\（多模块包）
echo 产物：build_output\Proxy_Manager_Studio.dist\
echo 说明：pms 包、core 核心目录与 icon.ico 会一并打包；data\logs 为运行期目录。
echo.

nuitka --standalone ^
    --enable-plugin=pyside6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --include-data-files=icon.ico=icon.ico ^
    --include-data-files=.env.example=.env.example ^
    --include-data-files=软件升级迭代记录.md=软件升级迭代记录.md ^
    --include-data-dir=core=core ^
    --include-package=pms ^
    --include-package=requests ^
    --include-package=certifi ^
    --include-package=urllib3 ^
    --include-package=charset_normalizer ^
    --include-package=idna ^
    --include-package=yaml ^
    --include-package=qrcode ^
    --include-package=psutil ^
    --follow-imports ^
    --jobs=4 ^
    --clang ^
    --remove-output ^
    --output-dir=build_output ^
    Proxy_Manager_Studio.py

rem NOTE (v1.1.0) 多模块架构下的打包要点：
rem   1. 入口是启动器 Proxy_Manager_Studio.py，实现全在 pms\ 包里。
rem      --include-package=pms 显式带上主包；--follow-imports 也会自动收集（双保险）。
rem   2. 新增模块后无需改本脚本（整包收集）；但若新增了对第三方库的导入，
rem      记得在这里补 --include-package=<库名>（或确认 follow-imports 能收集到）。
rem   3. --enable-plugin=pyside6 会自动收集 Qt 插件；本程序只用 QtCore/QtGui/QtWidgets/QtNetwork，
rem      没有用到 QtMultimedia，因此不需要 --include-qt-plugins=multimedia。
rem   4. --include-data-dir=core=core 会把 xray.exe / sing-box.exe 打进去；若不需要内置核心，
rem      删掉该参数即可（程序启动后可在「设置 → 核心 → 下载 / 更新」里自动获取）。
rem   5. 运行期数据（pms_settings.json、pms_store.db、logs、data\configs）写在 exe 同级目录，
rem      因此建议把 dist 目录放到有写权限的位置（不要放在 Program Files）。
rem   6. 自检命令：
rem        Proxy_Manager_Studio.exe --version      查看版本
rem        Proxy_Manager_Studio.exe --selfcheck    查看根目录 / 核心 / .env / 图标检测结果
echo.
echo 打包完成。建议接着运行：Proxy_Manager_Studio.exe --selfcheck
pause
