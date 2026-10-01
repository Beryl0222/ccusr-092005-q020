"""启动入口：``python -m broth``。

环境变量：
  BROTH_DB        批次档案 JSON 路径（默认 data/broth_db.json）
  BROTH_HOST      监听地址（默认 127.0.0.1）
  BROTH_PORT      端口（默认 8080）
  BROTH_TOKENS_FILE  令牌映射 JSON
"""

from __future__ import annotations

import os

from .api import build_server


def main() -> None:
    host = os.environ.get("BROTH_HOST", "127.0.0.1")
    port = int(os.environ.get("BROTH_PORT", "8080"))
    db_path = os.environ.get("BROTH_DB", "data/broth_db.json")
    server = build_server(host, port, db_path)
    print(f"老汤批次服务监听 http://{host}:{port}，档案 {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
