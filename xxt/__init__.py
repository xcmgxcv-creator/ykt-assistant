"""雨课堂小助手 —— 只读抓取雨课堂网页端的作业与公告，桌面悬浮清单提醒。

本项目基于 led629263224-cmyk/xxt-assistant（学习通小助手）改造，上游仓库
未附 LICENSE：这里保留其界面、提醒引擎、SQLite 存储与打包脚本，替换掉平台
数据层（原 xxt/client.py 与 xxt/parse.py 已删除）。

包内模块：
    crypto      Windows DPAPI 会话文件加解密
    config      路径与配置读写
    store       SQLite 存储与增量 diff
    sync        同步编排（抓取 -> 入库 -> 产生事件）
    notify      桌面气泡提醒与去重
    rainclass   雨课堂数据层（零第三方依赖的解析 + 请求客户端 + 扫码登录）
    ui          PySide6 界面（悬浮清单 / 托盘 / 设置对话框）
"""

__version__ = "0.1.0"
APP_NAME = "雨课堂小助手"
