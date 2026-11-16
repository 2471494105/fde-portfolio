"""这条结果属于哪个代码版本。

代码基线（Git）和运行时轨迹（runs/*.jsonl）的接点。没有它，93 条轨迹只能说明
"某天跑过"，不能说明"哪个版本跑过"——出了回归连是哪个改动引入的都定位不了。

刻意不 shell 出去调 git：这台机器 git 装了但不在 PATH（实测 `git` 命令不可用），
所以直接读 .git 里的文件，任何异常都返回空串，绝不拖累主流程。
"""

import functools
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 决定模型行为的文件：改其中任何一个都可能让历史结论失效
BEHAVIOR_FILES = (
    "app/graph/nodes.py",
    "app/graph/llm.py",
    "app/tools/schemas.py",
    "app/tools/workorder.py",
)


@functools.lru_cache(maxsize=1)
def git_commit(short: int = 7) -> str:
    """取当前 commit 前短位。取不到返回空串。

    带缓存：一个进程生命周期内 commit 不会变。这也顺带给了它一个正确的语义——
    长驻的 Web 服务改了代码但不重启，轨迹里记的仍是旧 commit，因为跑的确是旧代码。
    """
    try:
        d = ROOT
        for _ in range(6):
            g = d / ".git"
            if g.is_dir():
                head = (g / "HEAD").read_text(encoding="utf-8").strip()
                if not head.startswith("ref:"):
                    return head[:short]
                ref = head.split(":", 1)[1].strip()
                f = g / ref
                if f.is_file():
                    return f.read_text(encoding="utf-8").strip()[:short]
                # 打包过的引用（git gc 之后分支信息会进 packed-refs）
                packed = g / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            return parts[0][:short]
                return ""
            d = d.parent
    except Exception:
        return ""
    return ""


@functools.lru_cache(maxsize=1)
def code_hash() -> str:
    """决定模型行为的文件指纹（12 位）。

    刻意不单抽 prompt 常量：正则会因为改个变量名就失效，而漏掉的部分
    （schemas 的 enum、workorder 的枚举白名单）本来也是模型可见行为——
    枚举写错会改幂等键，同一张单会被建两次。整文件 hash 覆盖更全，
    代价是改注释也会变：宁可误报"代码变了"，也不要漏报"prompt 被改过"。
    """
    h = hashlib.sha256()
    for rel in BEHAVIOR_FILES:
        try:
            h.update((ROOT / rel).read_bytes())
        except Exception:
            h.update(rel.encode("utf-8"))
    return h.hexdigest()[:12]


def stamp() -> dict:
    """一次 run 要记的版本标识。"""
    commit = git_commit()
    return {"commit": commit, "code_hash": code_hash()}
