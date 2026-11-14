"""CACH-0002 缓存预热与刷新批次验收脚本。

覆盖验收口径：
1. 预热后查询结果快照可见，并带新鲜/过期判定原因；
2. 过期后立即标记失效（用 1 秒 TTL 的预热批次验证）；
3. 刷新失败不能把旧快照标成成功；
4. 刷新策略变更不复用旧版本（键版本推进、旧快照失效）；
5. 上游路由目标服务切换后，缓存键名版本跟着更新；
6. 预热批次可重复执行，但只保留一份有效结果；
7. 批次视图按时间倒序、最近一次生成排在最前。

用法（先启动后端）：
    python -I tools/verify_cache.py http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
CACHE_ID = 2  # CACH-0002
ROUTER_ID = 2  # ROUT-0002，目标服务与 CACH-0002 来源服务一致


def call(method: str, path: str, values: dict | None = None) -> dict:
    body = json.dumps({"values": values or {}}).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=body if method != "GET" else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 业务失败也返回体，取出来便于断言
        return json.loads(exc.read().decode("utf-8"))


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        raise SystemExit(f"验收未通过：{name}")


def active_snapshot() -> dict | None:
    return call("GET", f"/api/cache/{CACHE_ID}/snapshots").get("active")


def main() -> None:
    print("== 0. 核对 CACH-0002 配置字段 ==")
    entry = call("GET", f"/api/cache/{CACHE_ID}")
    check("缓存键名存在", bool(entry.get("缓存键名")), entry["缓存键名"])
    check("过期秒数为 60", str(entry.get("过期秒数")) == "60", entry["过期秒数"])
    check("刷新策略为 TTL 定时刷新", entry.get("刷新策略") == "TTL 定时刷新", entry["刷新策略"])

    print("== 1. 预热后查询结果快照可见 ==")
    prewarm = call("POST", f"/api/cache/{CACHE_ID}/prewarm")
    check("预热批次成功", prewarm.get("ok") is True, prewarm.get("message", ""))
    snapshot = active_snapshot()
    check("预热后存在有效快照", snapshot is not None)
    check("快照带来源服务版本", bool(snapshot and snapshot.get("source_version")))
    check("新鲜判定原因可见", bool(snapshot and "判定为新鲜" in snapshot.get("freshness_reason", "")))
    first_no = snapshot["snapshot_no"]

    print("== 2. 模拟命中回写最近命中 ==")
    hit = call("POST", f"/api/cache/{CACHE_ID}/hit")
    check("模拟命中成功", hit.get("ok") is True, hit.get("message", ""))
    hit_snapshot = active_snapshot()
    check("命中次数累加为 1", hit_snapshot and hit_snapshot.get("hit_count") >= 1)
    check("最近命中时间已回写", bool(hit_snapshot and hit_snapshot.get("last_hit_at")))

    print("== 3. 重复预热只保留一份有效结果 ==")
    call("POST", f"/api/cache/{CACHE_ID}/prewarm")
    snapshots = call("GET", f"/api/cache/{CACHE_ID}/snapshots")["items"]
    fresh = [item for item in snapshots if item["snapshot_state"] == "有效"]
    check("有效快照始终只有一份", len(fresh) == 1, f"有效数={len(fresh)}")
    check("旧快照已被顶替为失效", any(item["snapshot_no"] == first_no and item["snapshot_state"] == "已失效" for item in snapshots))

    print("== 4. 过期后立即标记失效 ==")
    call("POST", f"/api/cache/{CACHE_ID}/prewarm", {"过期秒数": 1})
    soon = active_snapshot()
    check("1 秒 TTL 快照先为有效", soon is not None and soon["ttl_seconds"] == 1)
    time.sleep(1.2)
    after_expire = call("GET", f"/api/cache/{CACHE_ID}/snapshots")
    check("到期后无有效快照", after_expire.get("active") is None)
    expired = next(item for item in after_expire["items"] if item["snapshot_no"] == soon["snapshot_no"])
    check("到期快照立即标记过期", expired["snapshot_state"] == "已过期", expired["freshness_reason"])
    check("过期判定原因可见", "判定为过期" in expired["freshness_reason"])
    batches = call("GET", f"/api/cache/{CACHE_ID}/batches")
    check("过期失效批次已登记", any(item["batch_type"] == "过期失效" for item in batches["items"]))

    print("== 5. 刷新失败不能把旧快照标成成功 ==")
    call("POST", f"/api/cache/{CACHE_ID}/prewarm", {"过期秒数": 120})
    good = active_snapshot()
    failed = call("POST", f"/api/cache/{CACHE_ID}/refresh", {"模拟失败": True})
    check("刷新接口如实返回失败", failed.get("ok") is False, failed.get("message", ""))
    still = active_snapshot()
    check("旧快照未被标成成功/替换", still is not None and still["snapshot_no"] == good["snapshot_no"])
    check("旧快照仍为有效且保留原判定", still["snapshot_state"] == "有效")
    failed_batches = [b for b in call("GET", f"/api/cache/{CACHE_ID}/batches")["items"] if b["batch_type"] == "刷新"]
    check("失败刷新批次结果为失败", failed_batches[0]["result"] == "失败")

    print("== 6. 刷新策略变更不复用旧版本 ==")
    version_before = call("GET", f"/api/cache/{CACHE_ID}")["缓存键版本"]
    policy = call("POST", f"/api/cache/{CACHE_ID}/refresh-policy", {"刷新策略": "主动刷新+灰度校验"})
    check("策略变更成功", policy.get("ok") is True, policy.get("message", ""))
    version_after = call("GET", f"/api/cache/{CACHE_ID}")["缓存键版本"]
    check("缓存键版本已推进", version_after == version_before + 1, f"v{version_before} -> v{version_after}")
    check("策略变更后无旧版本有效快照", active_snapshot() is None)
    old_version_snapshots = [
        item for item in call("GET", f"/api/cache/{CACHE_ID}/snapshots")["items"]
        if item["key_version"] == version_before
    ]
    check("旧版本快照全部失效", all(item["snapshot_state"] == "已失效" for item in old_version_snapshots))
    re_prewarm = call("POST", f"/api/cache/{CACHE_ID}/prewarm")
    check("新版本可重新预热", re_prewarm.get("ok") is True)
    check("新快照键版本为新版本", active_snapshot()["key_version"] == version_after)

    print("== 7. 路由目标服务切换，缓存键版本联动更新 ==")
    rule_before = call("GET", f"/api/router/{ROUTER_ID}")
    old_target, new_target = rule_before["目标服务"], "订单查询服务-v2"
    linked = call("POST", f"/api/router/{ROUTER_ID}/target", {"目标服务": new_target})
    check("路由目标切换成功", linked.get("ok") is True, linked.get("message", ""))
    affected = linked["entry"]["affected_cache"]
    check("联动命中 CACH-0002", any(item["id"] == CACHE_ID for item in affected))
    entry_now = call("GET", f"/api/cache/{CACHE_ID}")
    check("来源服务跟随更新", entry_now["来源服务"] == new_target)
    check("缓存键版本再次推进", entry_now["缓存键版本"] == version_after + 1)
    check("切换后旧快照不再有效", active_snapshot() is None)
    final_prewarm = call("POST", f"/api/cache/{CACHE_ID}/prewarm")
    check("切换后预热使用新来源版本", final_prewarm["entry"]["source_version"] == new_target)

    print("== 8. 批次视图按时间倒序、最近一次生成在最前 ==")
    batch_view = call("GET", f"/api/cache/{CACHE_ID}/batches")
    times = [item["generated_at"] for item in batch_view["items"]]
    check("批次按时间倒序", times == sorted(times, reverse=True))
    check("最近一次生成呈现为 latest", batch_view["latest"]["batch_no"] == batch_view["items"][0]["batch_no"])
    check("批次类型覆盖预热/刷新/过期/版本推进", {
        "预热", "刷新", "过期失效", "版本推进",
    }.issubset({item["batch_type"] for item in batch_view["items"]}))

    print("\n全部验收通过 ✅  CACH-0002 缓存键名 =", entry_now["缓存键名"])


if __name__ == "__main__":
    main()
