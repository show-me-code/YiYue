"""全局路径与常量。"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # backend/
# 测试可通过 YIYUE_DATA_DIR 重定向数据目录（须在导入 app 包前设置）
DATA_DIR = Path(os.environ.get("YIYUE_DATA_DIR", ROOT / "data")).resolve()
TASKS_DIR = DATA_DIR / "tasks"
DB_PATH = DATA_DIR / "app.db"
DB_URL = f"sqlite:///{DB_PATH}"

MAX_UPLOAD_MB = 200
PART_RETRY_TIMES = 2
RETRY_BACKOFF_SECONDS = (2, 8)

for _p in (DATA_DIR, TASKS_DIR):
    _p.mkdir(parents=True, exist_ok=True)
