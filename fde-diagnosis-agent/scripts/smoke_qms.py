"""mock QMS 的集成测试：正常建单、幂等重复、客户端前置校验、服务端业务码兜底。

护栏前移之后，severity 非法 / 动作为空这类入参问题在客户端就被拦住了，不会到达 QMS。
所以这里刻意分两组：
  A. 走 create_work_order，验证"校验 -> 建单 -> 去重"的闭环；
  B. 绕过客户端直接打 HTTP，验证"万一护栏失效，服务端仍然用 code 而不是 HTTP 状态码表达失败"。
两组都要有，因为客户端校验和服务端兜底是两道独立的门。
"""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tools.workorder import QMS_URL, create_work_order  # noqa: E402


def main():
    base = dict(
        device_id="CX-102",
        root_cause="物料批次 B20261105 引入新纹理，模型未覆盖",
        root_cause_code="batch_drift",
        severity="medium",
        actions=["补充该批次样本重训", "临时提高该类缺陷阈值并人工复判"],
        evidence_refs=["B20261105 误判率48.6%", "其他批次≤16.7%"],
    )

    print("--- 1. 正常建单 ---")
    r1 = create_work_order(**base)
    print(r1)

    print("\n--- 2. 重复建单（应命中幂等）---")
    r2 = create_work_order(**base)
    print(r2)

    print("\n--- 3. 非法 severity：客户端前置校验就拦住，不发请求 ---")
    r3 = create_work_order(**{**base, "severity": "urgent"})
    print(r3)

    print("\n--- 4. 空处置动作：客户端前置校验拦住 ---")
    r4 = create_work_order(**{**base, "actions": []})
    print(r4)

    print("\n--- 5. 空证据：客户端前置校验拦住（有证据才能建单）---")
    r5 = create_work_order(**{**base, "evidence_refs": []})
    print(r5)

    print("\n--- 6. 绕过客户端直连 QMS：服务端仍返回 HTTP 200 + 业务码 4001 ---")
    resp = httpx.post(f"{QMS_URL}/api/work-order", json={**base, "severity": "urgent"}, timeout=10)
    body = json.loads(resp.text)
    print(f"http={resp.status_code} code={body.get('code')} message={body.get('message')}")

    ok = (
        r1.get("order_id") and r1.get("deduped") is False
        and r2.get("deduped") is True and r2.get("order_id") == r1.get("order_id")
        and r3.get("error") and r3.get("problems")
        and r4.get("error") and r4.get("problems")
        and r5.get("error") and r5.get("problems")
        and resp.status_code == 200 and body.get("code") == 4001
    )
    print("\n[结果]", "全部通过" if ok else "有检查项未通过")


if __name__ == "__main__":
    main()
