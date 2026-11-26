"""跑全量之前的现场自检：库是种子态、服务都在听、事实层没有缺口。

**为什么不是 `eval/run_eval.py` 里的一段。**
`eval/run_eval.py` 在 `app/meta.py` 的 `BEHAVIOR_FILES` 里——判据口径也算"行为"。
往它里面加三十行自检，`code_hash` 立刻就变，磁盘上那份 `1547` 全量当场作废，
为了一个"跑之前就知道能不能跑"的功能，要再付 373 秒。而这份脚本只读文件、只连库，
不在指纹里，加它一分钱不花，`1547` 仍然代表当前代码。

**它拦的是什么（都是真发生过的）。**
- 8010 没起：`create_work_order` 全返回 WinError 10061，`W3` 被记成 `owner=model`，
  summary 还写着 `env_n=0`——环境故障记到模型头上（`DOC-50`）。
- 库不是种子态：提交前实测 `work_orders=4`、`defect_logs=9`，而文档写着"已复位"。
  那次没有判据，"我记得做过"就是全部依据（`N-21`）。
- 事实层缺口：`client_a` 只有 27 行/4 台时，CX-105 的任何结论都不可能被回指，
  评测却只会记"模型没写出处"（`N-16`）。

**它不拦什么。**
模型会答成什么样，这份脚本一个字都不判——那是评测的事。这里只回答一个问题：
**这 373 秒花下去，算出来的数字有没有意义。**

用法：
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\preflight.py          # 全量前
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\preflight.py --only=W3  # 只跑建单类
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\preflight.py --skip-qms  # 明知要停 QMS 的反例
    .\\venv\\Scripts\\python.exe -X utf8 scripts\\preflight.py --ports 5432,11434,9999  # 自检本脚本

退出码：0 = 可以跑；1 = 拒跑（逐条打印为什么）；2 = 连不上库（跟 1 分开，别把
"数据库没起"混进"库不是种子态"，两者的处置完全不同）。
"""

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DSN = "postgresql://postgres:postgres@localhost:5432/fde_rag"
SBX_DSN = "postgresql://postgres:postgres@localhost:5432/fde_sandbox"

# 种子 002_data.sql 落库后的行数。
#
# **这张表是手写的，这是它唯一的弱点**：改种子而忘了改这里，自检会红——
# 但那是"红得对"（底下确实变了），比沉默地放过要好。为了让它不至于在改种子后
# 变成一句永远为假的告警，下面同时比对 seed_hash：种子一变就拒跑并要求一起改数，
# 而不是拿旧期望去审新库。
SEED_COUNTS = {"devices": 5, "work_orders": 1, "defect_logs": 1000, "alarms": 8}
EXPECTED_SEED_HASH = "9caa1322fbfc"

# 事实层覆盖：client_a 是评测用的生产租户，7 个属性 × 设备数。
FACT_TENANT = "client_a"
FACT_KEYS = ("line", "station", "camera_model", "lens_model",
             "light_model", "install_date", "maintenance_cycle_days")

PORTS = ((5432, "PostgreSQL"), (11434, "Ollama"), (8010, "mock QMS"))


def _port_open(port, host="127.0.0.1", timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_ports(ports):
    bad = []
    for port, name in ports:
        ok = _port_open(port)
        print(f"  {'OK ' if ok else '关 '} {port:<6} {name}")
        if not ok:
            bad.append((port, name))
    return bad


def _counts(sbx):
    with sbx.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM public.devices) AS devices,"
                    " (SELECT count(*) FROM public.work_orders) AS work_orders,"
                    " (SELECT count(*) FROM public.defect_logs) AS defect_logs,"
                    " (SELECT count(*) FROM public.alarms) AS alarms")
        return {k: int(v) for k, v in cur.fetchone().items()}


# ---- 以下是纯函数：判定逻辑与 IO 分开，才能在单测里把每种"该拒跑"的形状走一遍 ----

def seed_problems(counts, got_seed_hash, want_seed_hash=EXPECTED_SEED_HASH,
                  want_counts=None):
    """库态与种子态不一致的地方，返回人话列表（空 = 没问题）。

    种子指纹先判：种子变了，行数期望就失效。先判这条，才能避免把
    "种子换了一版"报成"上一轮没复位"——那会让人去复位一个本来就不是旧版的库。
    """
    want_counts = want_counts or SEED_COUNTS
    out = []
    if got_seed_hash != want_seed_hash:
        out.append(
            f"seed_hash 变成 {got_seed_hash}（本脚本记的是 {want_seed_hash}）："
            "种子改过，SEED_COUNTS 那四个数需要重新核对再改，别拿旧期望审新库")
    for k, want in want_counts.items():
        got = counts.get(k)
        if got != want:
            out.append(
                f"{k}={got}，种子态应为 {want}：上一轮跑完没有复位。"
                "复位：psql -d fde_sandbox -f data\\seed\\002_data.sql")
    return out


def coverage_problems(expected, missing):
    """事实层缺口。只判"缺"，不判"多"——多出来的键不影响回指，只说明库里有历史。"""
    if not missing:
        return []
    shown = ", ".join(missing[:6]) + ("…" if len(missing) > 6 else "")
    return [
        f"事实层缺口 {shown}（应有 {expected} 个键）：这些键不可能被回指，"
        "评测会把它记成模型没写出处（N-16）。"
        "补：venv\\Scripts\\python.exe scripts\\check_facts.py --seed-prod --seed-from=devices"]


def _device_facts(sbx, prod):
    """每台设备 × 7 个属性，返回 (有值属性数, 缺回指的键)。

    只读：不从 devices 推 facts，也不修任何东西——自检一旦会写库，它自己就成了一条
    需要被检查的链路。
    """
    with sbx.cursor() as cur:
        cur.execute("SELECT device_id, line, station, camera_model, lens_model,"
                    " light_model, install_date, maintenance_cycle_days FROM public.devices")
        devices = cur.fetchall()
    with prod.cursor() as cur:
        cur.execute("SELECT entity_id, fact_key FROM facts.device_fact_current"
                    " WHERE tenant = %s", (FACT_TENANT,))
        have = {(r["entity_id"], r["fact_key"]) for r in cur.fetchall()}

    expected = 0
    missing = []
    for d in devices:
        for k in FACT_KEYS:
            if d.get(k) is None or str(d.get(k)).strip() == "":
                continue  # 空值本来就没有可回指的键，不算缺口
            expected += 1
            if (d["device_id"], k) not in have:
                missing.append(f"{d['device_id']}.{k}")
    return expected, missing


def main():
    ap = argparse.ArgumentParser(description="全量前现场自检")
    ap.add_argument("--skip-qms", action="store_true",
                    help="明知 8010 要停（跑环境反例）时用；会在输出里留一行 !!")
    ap.add_argument("--ports", default=None,
                    help="自定义端口表，形如 5432,11434,8010（用来自检本脚本）")
    ap.add_argument("--skip-db", action="store_true", help="只查端口（库还在起的时候）")
    args = ap.parse_args()

    ports = PORTS
    if args.ports:
        ports = tuple((int(p), "自定义") for p in args.ports.split(","))
    if args.skip_qms:
        ports = tuple(p for p in ports if p[0] != 8010)

    failures = []
    print("== 端口 ==")
    bad = _check_ports(ports)
    for port, name in bad:
        if port == 8010:
            failures.append(
                "8010 没起：建单类用例会全部 10061，W3 那次就是这样被记成 owner=model 的。"
                "起法：venv\\Scripts\\python.exe -m uvicorn mock_qms.server:app --port 8010")
        elif port == 5432:
            failures.append("5432 没起：库连不上，先起 PostgreSQL")
        else:
            failures.append(f"{port}（{name}）没起：模型调用会走失败分支，这一轮的数字不可比")

    if args.skip_db:
        return _report(failures, skipped_db=True)

    print("== 库 ==")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as e:  # pragma: no cover - 环境问题，不当判据
        print(f"  !! psycopg 不可用：{type(e).__name__}: {e}")
        return 2

    try:
        sbx = psycopg.connect(SBX_DSN, row_factory=dict_row, autocommit=True)
        prod = psycopg.connect(PROD_DSN, row_factory=dict_row, autocommit=True)
    except Exception as e:
        print(f"  !! 连不上库：{e}")
        return 2

    try:
        # 指纹先看：种子变了，下面的期望行数就没有意义了。
        from app.meta import code_hash, seed_hash
        ch, sh = code_hash(), seed_hash()
        print(f"  code_hash={ch}  seed_hash={sh}")
        counts = _counts(sbx)
        print("  行数 " + " ".join(f"{k}={counts[k]}" for k in SEED_COUNTS))
        failures.extend(seed_problems(counts, sh))

        expected, missing = _device_facts(sbx, prod)
        print(f"  事实覆盖 {FACT_TENANT}：应有 {expected} 个键，缺 {len(missing)} 个")
        failures.extend(coverage_problems(expected, missing))
    finally:
        sbx.close()
        prod.close()

    return _report(failures)


def _report(failures, skipped_db=False):
    if skipped_db:
        print("  （--skip-db：没查库，只清点端口）")
    if not failures:
        print("\n通过：可以跑。")
        return 0
    print("\n拒跑，%d 条：" % len(failures))
    for i, f in enumerate(failures, 1):
        print(f"  [{i}] {f}")
    print("\n先把上面几条清掉再跑——这 373 秒花在没准备好的现场上，算出来的数字没人能用。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
