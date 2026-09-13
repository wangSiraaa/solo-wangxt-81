"""测试环境：在导入 app 之前固定使用一次性 SQLite 数据库。"""
import os
import tempfile

_fd, _path = tempfile.mkstemp(suffix=".db")
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_path}"
