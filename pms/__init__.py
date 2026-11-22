#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代理节点管理控制台（Proxy Manager Studio）主包。

模块一览（按依赖顺序，详见各模块 docstring 与《软件升级迭代记录.md》第六章）：
    optional      可选依赖探测            constants    常量与设置默认值
    utils         通用工具函数            paths        运行路径解析
    settings      设置仓库（改动即保存）   envstore     凭据与环境变量
    logbus        日志总线                models       数据模型
    database      SQLite 仓储             sharelinks   分享链接解析/生成
    importers     配置导入                coreprofile  启动配置档
    coreconfig    核心配置生成            coresys      核心定位与系统集成
    statsapi      统计接口客户端          logparse     日志解析与连接追踪
    coreprocess   核心进程控制            tempsession  临时核心会话
    tasks         后台任务                latency      延迟测试调度
    speedtest     测速调度                traffic      流量与连接统计
    subscription  订阅更新                download     核心下载更新
    versioning    版本备份与单实例        theme        主题与样式
    icons         图标与二维码位图
    gui.*         界面（qtmodels / widgets / tray / dialogs.* / mainwindow）
    app           程序入口与装配

命名约定：类名前缀 Pms，方法名前缀 = 类前缀缩写（如 PmsSettingsManager → psm_），
模块级函数与常量统一 pms_ / PMS_ 前缀。所有类名与方法名全局唯一，避免命名冲突。
"""

from pms.constants import PMS_APP_NAME, PMS_APP_VERSION

__version__ = PMS_APP_VERSION
__all__ = ["PMS_APP_NAME", "PMS_APP_VERSION", "__version__"]
