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

# 会让历史结论失效的文件。判断标准不是"只算模型可见行为"，而是
# **改它之后，同一批用例跑出来的数字可能变**——图拓扑、state 字段结构、
# 判据口径，三者和 prompt 一样能让旧报表失去可比性。
BEHAVIOR_FILES = (
    "app/graph/nodes.py",
    # 图拓扑：路由函数的输出和 add_conditional_edges 的映射表决定能走哪些边。
    # 2026-11-19 的 KeyError: 'plan' 就是这张表少一个 key——那次修复改变了
    # 可观测结果（原来崩、现在跑得通），但它曾经不在这个列表里，hash 纹丝不动。
    "app/graph/build.py",
    # 字段结构：加一个字段就是多一份可观测输出。
    "app/graph/state.py",
    "app/graph/llm.py",
    "app/tools/schemas.py",
    "app/tools/workorder.py",
    # 工具实现。同一个 schema 背后一次能给多少，由这三份决定——2026-11-19
    # 修 A3 绕路时，schema 本身没说错，是 diagnosis.py 一条 SQL 只聚合一个维度。
    # 只登记 schemas 就会漏：改实现不动指纹，报告却继续声称"与旧基线可比"。
    "app/tools/diagnosis.py",
    "app/tools/manual.py",
    # hint 文案随响应回给模型，改它就改了模型下一轮看到的东西。
    "app/tools/hints.py",
    # 判据口径：改它不改模型，但会改"通过"的定义。kw_rate 假满分修正、
    # 标灰通过单列、规则净收益都在这份文件里——口径变了，数字就不能横着比。
    "eval/run_eval.py",
)

# 数据库初态指纹的输入。刻意与 BEHAVIOR_FILES 分开，不并进 code_hash：
# 前者是"代码怎么想"，后者是"库里有什么"，混成一条轴就分不清某次回归是改代码
# 还是改数据造成的。2026-11-20 补 work_orders 种子时暴露了这个缺口——
# 改动落在 data/ 下，code_hash 纹丝不动，于是 0013 和 2359 的 meta 除时间外
# 几乎一样，而数据库初态已经不同。判"两份报告能不能比"因此从三根轴变成四根。
#
# 只算**主文件**，不含 .bak_* 备份：备份随每次改种子新增一个，
# 算进去会让 hash 因为"多留了一份副本"而变化，与数据内容无关。
SEED_FILES = ("data/seed/002_data.sql",)


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


@functools.lru_cache(maxsize=1)
def seed_hash() -> str:
    """数据库初态指纹（12 位）。

    它回答一个 code_hash 答不了的问题：**这两份报告跑的是同一个库吗**。

    code_hash 只覆盖代码。种子里加一条工单、改一个误判概率，模型的行为和
    "通过"的定义都没变，但每条用例拿到的数据变了。以前这种改动在 meta 里
    完全不可见——报告看着"同一版本"，数字却已经不可横比。

    取不到文件时退化成文件名本身的 hash，与 code_hash 的处理一致：
    宁可给出一个"肯定不等于别人"的值，也不要让两个不同初态撞成同一个指纹。
    """
    h = hashlib.sha256()
    for rel in SEED_FILES:
        try:
            h.update((ROOT / rel).read_bytes())
        except Exception:
            h.update(rel.encode("utf-8"))
    return h.hexdigest()[:12]


def stamp() -> dict:
    """一次 run 要记的版本标识。

    三根轴：代码（commit + code_hash）、数据（seed_hash）、夹具（cases_hash
    在 run_eval 里单独算，因为夹具在 eval/ 下、不随代码走）。
    """
    commit = git_commit()
    return {"commit": commit, "code_hash": code_hash(), "seed_hash": seed_hash()}
