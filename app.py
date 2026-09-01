# -*- coding: utf-8 -*-
"""模型可用性监控服务入口。

启动后台探测线程 + HTTP 服务。配置由 config.py 从 .env 读取。
"""

import logging
import threading
from http.server import ThreadingHTTPServer

import config
import database
import http_server
import monitor


def main() -> None:
    """初始化数据库，启动后台探测线程与 HTTP 服务，并处理优雅关闭。

    @return None
    """
    # 配置全局日志格式与级别
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 初始化数据库
    database.init_db()

    # 启动后台探测线程（daemon，随进程退出）
    threading.Thread(target=monitor.monitor_loop, daemon=True).start()

    # 使用配置中的地址和端口启动 HTTP 服务
    server = ThreadingHTTPServer((config.HOST, config.PORT), http_server.Handler)
    logging.info("model-monitor listening on %s:%s", config.HOST, config.PORT)

    # 启动 HTTP 服务并支持 Ctrl+C 优雅关闭
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
