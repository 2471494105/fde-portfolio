"""数据库连接。

这一层的唯一职责是「拿到一个能用的连接」，不再关心连谁、怎么连——那交给
app/ds/ 下的数据源实现。以后换库、换形态只需要写一个新的 DataSource，
工具侧一行不用改。

签名和返回值刻意与原版逐字段保持一致：现存 5 处工具都写成

    with connect() as conn, conn.cursor() as cur:

其中 diagnosis.py / manual.py / workorder.py 在 BEHAVIOR_FILES 里。
在这里改签名，等于同时改三份指纹受保护的文件。
"""

from app.ds import get_source
from app.ds.pg import DSN  # 兼容旧引用：from app.tools.db import DSN


def connect():
    return get_source().connect()
