# -*- coding: utf-8 -*-
"""模型可用性监控服务入口。

启动后台探测线程 + HTTP 服务。配置由 config.py 从 .env 读取。
"""

import threading
from http.server import ThreadingHTTPServer

import config
import database
import http_server
import monitor


def main():
    """初始化数据库，启动后台探测线程与 HTTP 服务。"""
    database.init_db()
    # 启动后台探测线程（daeamon，随进程退出）
    threading.Thread(target=monitor.monitor_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), http_server.Handler)
    print("model-monitor listening on :", config.PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
