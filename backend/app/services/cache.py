"""结果缓存加速业务规则：状态流转、字段校验与筛选口径都收在这里。

除了缓存配置的维护，本服务还负责三类运行期对象的口径：

* 缓存快照（cache_snapshots）：某个缓存键名在一次预热/刷新后实际持有的查询结果，
  记录来源服务版本、生成时间、过期时间以及新鲜/过期判定原因；
* 预热与刷新批次（cache_batches）：每一次预热、刷新、失效与版本推进都登记为一个批次，
  按时间排列，最近一次生成排在最前；
* 缓存键版本：刷新策略变更或上游路由目标服务切换时，键版本必须推进，
  旧版本快照立即失效，避免灰度规则复用旧结果。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from app.store import store

MODULE = "cache"
BATCH_MODULE = "cache_batches"
SNAPSHOT_MODULE = "cache_snapshots"
REQUIRED_FIELDS = ["配置编号", "缓存键名", "过期秒数"]
STATUS_ORDER = ["草稿", "生效中", "已暂停", "已关闭"]
ACTION_RULES = {"启用缓存": "草稿", "暂停缓存": "生效中", "关闭缓存": "已暂停"}
NEGATIVE_ACTIONS = []

# 快照生命周期状态：有效/已过期/已失效（版本推进、预热替换、配置关闭等主动动作）。
SNAPSHOT_FRESH = "有效"
SNAPSHOT_EXPIRED = "已过期"
SNAPSHOT_INVALID = "已失效"

BATCH_PREWARM = "预热"
BATCH_REFRESH = "刷新"
BATCH_EXPIRE = "过期失效"
BATCH_VERSION = "版本推进"


def _now() -> datetime:
    """单独包一层时间源：测试里可以用虚拟时钟替换，验证秒级过期口径。"""
    return datetime.now()


def _format_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class CacheService:
    """缓存配置 + 预热/刷新批次 + 查询结果快照的统一业务入口。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _now

    # ------------------------------------------------------------------ 配置
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["刷新策略"] = values.get("刷新策略") or "TTL 定时刷新"
        entry["来源服务"] = values.get("来源服务") or "未指定来源服务"
        entry["缓存键版本"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结果缓存加速可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 关闭缓存意味着该键名不再对外提供快照，存量快照统一作废。
        # 动作链里「关闭缓存」落到「已暂停」之后的收尾状态，这里按动作本身判定。
        if action == "关闭缓存":
            self._invalidate_snapshots(
                entry_id,
                reason="缓存配置已关闭，存量快照不再对外提供",
                batch_type=BATCH_VERSION,
            )
        return entry, f"缓存配置已{action}"

    # ------------------------------------------------------------- 刷新策略
    def change_refresh_policy(self, entry_id: int, new_policy: str) -> tuple[dict[str, Any] | None, str]:
        """刷新策略变更：策略口径变化后旧版本结果不能复用，键版本推进并作废旧快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存配置 {entry_id} 不存在或已归档"
        policy = str(new_policy or "").strip()
        if not policy:
            return None, "刷新策略不能为空"
        old_policy = entry.get("刷新策略")
        if old_policy == policy:
            return entry, "刷新策略与当前配置一致，无需变更"
        entry["刷新策略"] = policy
        self._bump_key_version(
            entry,
            reason=f"刷新策略由「{old_policy}」变更为「{policy}」，旧版本结果不得复用",
        )
        return entry, f"刷新策略已变更为「{policy}」，缓存键版本推进至 v{entry['缓存键版本']}"

    # --------------------------------------------------------------- 批次视图
    def list_batches(self, entry_id: int) -> list[dict[str, Any]]:
        """返回该配置的预热/刷新/失效批次，按生成时间倒序，最近一次生成在最前。"""
        if store.find(MODULE, entry_id) is None:
            return []
        rows = [
            dict(row)
            for row in store.rows(BATCH_MODULE)
            if int(row.get("cache_id", 0)) == entry_id
        ]
        rows.sort(key=lambda row: str(row.get("generated_at", "")), reverse=True)
        return rows

    def latest_batch(self, entry_id: int) -> dict[str, Any] | None:
        batches = self.list_batches(entry_id)
        return batches[0] if batches else None

    # --------------------------------------------------------------- 快照视图
    def list_snapshots(self, entry_id: int) -> list[dict[str, Any]]:
        """按缓存键名返回快照，并在读取时即时刷新过期判定与判定原因。"""
        if store.find(MODULE, entry_id) is None:
            return []
        now = self._clock()
        result: list[dict[str, Any]] = []
        for row in store.rows(SNAPSHOT_MODULE):
            if int(row.get("cache_id", 0)) != entry_id:
                continue
            snapshot = dict(row)
            self._evaluate_snapshot(snapshot, now)
            result.append(snapshot)
        result.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        return result

    def active_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        """当前唯一一份有效快照；预热可重复执行，但同一键名只保留一份有效结果。"""
        for snapshot in self.list_snapshots(entry_id):
            if snapshot.get("snapshot_state") == SNAPSHOT_FRESH:
                return snapshot
        return None

    # ------------------------------------------------------------- 模拟命中
    def simulate_hit(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """模拟一次查询命中：命中的是当前有效快照，并回写最近命中时间与命中次数。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存配置 {entry_id} 不存在或已归档"
        now = self._clock()
        for row in store.rows(SNAPSHOT_MODULE):
            if int(row.get("cache_id", 0)) != entry_id:
                continue
            self._evaluate_snapshot(row, now)
            if row.get("snapshot_state") == SNAPSHOT_FRESH:
                row["hit_count"] = int(row.get("hit_count", 0)) + 1
                row["last_hit_at"] = _format_time(now)
                self._append_batch(
                    entry_id,
                    batch_type="模拟命中",
                    result="命中",
                    detail=(
                        f"命中快照 {row['snapshot_no']}（键版本 v{row['key_version']}），"
                        f"来源服务版本 {row['source_version']}"
                    ),
                    snapshot_no=row["snapshot_no"],
                )
                return dict(row), "查询命中有效快照，已回写最近命中时间"
        self._append_batch(
            entry_id,
            batch_type="模拟命中",
            result="未命中",
            detail="当前没有有效快照，查询将回源并等待下一次预热/刷新",
        )
        return None, "未命中：该缓存键名当前没有有效快照"

    # ------------------------------------------------------------------ 预热
    def prewarm(
        self,
        entry_id: int,
        *,
        ttl_seconds: int | None = None,
        fail: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行预热批次：成功才写入新快照；重复预热会顶替旧快照，只保留一份有效结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存配置 {entry_id} 不存在或已归档"
        ttl = self._resolve_ttl(entry, ttl_seconds)
        if ttl <= 0:
            return None, "过期秒数必须为正整数，无法按该口径预热"
        now = self._clock()
        if fail:
            batch = self._append_batch(
                entry_id,
                batch_type=BATCH_PREWARM,
                result="失败",
                detail="模拟来源服务不可用，预热未取到查询结果，旧快照保持原状、不标记成功",
            )
            return None, f"预热批次 {batch['batch_no']} 失败：来源服务不可用，旧快照未被改写"

        key_version = int(entry.get("缓存键版本", 1))
        # 重复预热：先把同键名的旧有效快照顶掉，保证只保留一份有效结果。
        self._invalidate_snapshots(
            entry_id,
            reason=f"预热批次重新生成键版本 v{key_version} 的快照，旧快照被顶替",
            batch_type=BATCH_PREWARM,
            skip_missing_batch=True,
        )
        source_version = str(entry.get("来源服务") or "未指定来源服务")
        snapshot = self._append_snapshot(
            entry,
            key_version=key_version,
            source_version=source_version,
            ttl_seconds=ttl,
            now=now,
            trigger=BATCH_PREWARM,
        )
        self._append_batch(
            entry_id,
            batch_type=BATCH_PREWARM,
            result="成功",
            detail=(
                f"预热生成快照 {snapshot['snapshot_no']}，来源服务版本 {source_version}，"
                f"有效期 {ttl} 秒，将于 {snapshot['expire_at']} 到期"
            ),
            snapshot_no=snapshot["snapshot_no"],
        )
        return snapshot, f"预热成功：快照 {snapshot['snapshot_no']} 已生效"

    # ------------------------------------------------------------------ 刷新
    def refresh(
        self,
        entry_id: int,
        *,
        ttl_seconds: int | None = None,
        fail: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行刷新批次：刷新失败时绝不能把旧快照标成成功，旧快照维持原有状态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存配置 {entry_id} 不存在或已归档"
        ttl = self._resolve_ttl(entry, ttl_seconds)
        if ttl <= 0:
            return None, "过期秒数必须为正整数，无法按该口径刷新"
        now = self._clock()
        previous = self.active_snapshot(entry_id)
        if fail:
            previous_no = previous["snapshot_no"] if previous else "无有效快照"
            self._append_batch(
                entry_id,
                batch_type=BATCH_REFRESH,
                result="失败",
                detail=(
                    f"模拟来源服务返回 5xx，刷新失败；旧快照 {previous_no} "
                    "维持原状态，绝不标记为刷新成功"
                ),
                snapshot_no=previous.get("snapshot_no") if previous else None,
            )
            return None, "刷新失败：来源服务返回错误，旧快照未被标记为成功"

        key_version = int(entry.get("缓存键版本", 1))
        source_version = str(entry.get("来源服务") or "未指定来源服务")
        # 成功刷新同样顶替旧快照：每个键名同一时刻只保留一份有效结果。
        self._invalidate_snapshots(
            entry_id,
            reason=f"刷新批次重新拉取来源服务 {source_version}，旧快照被顶替",
            batch_type=BATCH_REFRESH,
            skip_missing_batch=True,
        )
        snapshot = self._append_snapshot(
            entry,
            key_version=key_version,
            source_version=source_version,
            ttl_seconds=ttl,
            now=now,
            trigger=BATCH_REFRESH,
        )
        self._append_batch(
            entry_id,
            batch_type=BATCH_REFRESH,
            result="成功",
            detail=(
                f"刷新生成快照 {snapshot['snapshot_no']}，来源服务版本 {source_version}，"
                f"有效期 {ttl} 秒，将于 {snapshot['expire_at']} 到期"
            ),
            snapshot_no=snapshot["snapshot_no"],
        )
        return snapshot, f"刷新成功：新快照 {snapshot['snapshot_no']} 已生效"

    # ------------------------------------------------------- 上游路由联动
    def on_router_target_changed(self, old_target: str, new_target: str) -> list[dict[str, Any]]:
        """路由服务切换目标服务后联动：命中旧目标服务的缓存键版本推进、旧快照立即失效。"""
        affected: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            if str(entry.get("来源服务") or "") != old_target:
                continue
            entry["来源服务"] = new_target
            self._bump_key_version(
                entry,
                reason=(
                    f"上游路由目标服务由「{old_target}」切换为「{new_target}」，"
                    "灰度规则不得复用旧版本查询结果"
                ),
            )
            affected.append(dict(entry))
        return affected

    # ------------------------------------------------------------- 内部工具
    def _resolve_ttl(self, entry: dict[str, Any], override: int | None) -> int:
        if override is not None:
            return int(override)
        try:
            return int(str(entry.get("过期秒数") or "0").strip())
        except ValueError:
            return 0

    def _next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1

    def _append_snapshot(
        self,
        entry: dict[str, Any],
        *,
        key_version: int,
        source_version: str,
        ttl_seconds: int,
        now: datetime,
        trigger: str,
    ) -> dict[str, Any]:
        expire_at = now + timedelta(seconds=ttl_seconds)
        cache_id = int(entry["id"])
        serial = sum(
            1
            for row in store.rows(SNAPSHOT_MODULE)
            if int(row.get("cache_id", 0)) == cache_id
        ) + 1
        snapshot = {
            "id": self._next_id(SNAPSHOT_MODULE),
            "cache_id": cache_id,
            "snapshot_no": f"SNAP-{int(entry['id']):04d}-{serial:03d}",
            "cache_key": entry.get("缓存键名"),
            "key_version": key_version,
            "source_version": source_version,
            "trigger": trigger,
            "ttl_seconds": ttl_seconds,
            "generated_at": _format_time(now),
            "expire_at": _format_time(expire_at),
            "last_hit_at": None,
            "hit_count": 0,
            # 模拟批次生成时携带的当前字段值，便于管理员核对快照内容。
            "payload": {
                "缓存键名": entry.get("缓存键名"),
                "缓存键版本": f"v{key_version}",
                "来源服务版本": source_version,
                "刷新策略": entry.get("刷新策略"),
                "过期秒数": ttl_seconds,
                "模拟字段": {
                    "order_id": str(entry.get("缓存键名", "")).split(":")[-1] or "unknown",
                    "served_by": source_version,
                    "generated_at": _format_time(now),
                },
            },
            "snapshot_state": SNAPSHOT_FRESH,
            "freshness_reason": (
                f"快照生成于 {_format_time(now)}，TTL {ttl_seconds} 秒，"
                f"到期时间 {_format_time(expire_at)}，当前时间未超过到期时间，判定为新鲜"
            ),
        }
        store.rows(SNAPSHOT_MODULE).append(snapshot)
        return dict(snapshot)

    def _append_batch(
        self,
        entry_id: int,
        *,
        batch_type: str,
        result: str,
        detail: str,
        snapshot_no: str | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        table = store.rows(BATCH_MODULE)
        # 批次编号按缓存配置各自递增，避免不同配置共用全局计数器造成跳号。
        own_count = sum(1 for row in table if int(row.get("cache_id", 0)) == entry_id)
        batch = {
            "id": self._next_id(BATCH_MODULE),
            "cache_id": entry_id,
            "batch_no": f"BATCH-{entry_id:04d}-{own_count + 1:03d}",
            "batch_type": batch_type,
            "result": result,
            "snapshot_no": snapshot_no,
            "detail": detail,
            "generated_at": _format_time(now),
        }
        table.append(batch)
        return dict(batch)

    def _invalidate_snapshots(
        self,
        entry_id: int,
        *,
        reason: str,
        batch_type: str,
        skip_missing_batch: bool = False,
    ) -> int:
        """把该键名下仍处于有效/已过期但未登记失效的快照统一置为已失效。"""
        now = self._clock()
        count = 0
        for row in store.rows(SNAPSHOT_MODULE):
            if int(row.get("cache_id", 0)) != entry_id:
                continue
            if row.get("snapshot_state") == SNAPSHOT_INVALID:
                continue
            row["snapshot_state"] = SNAPSHOT_INVALID
            row["freshness_reason"] = f"快照已失效：{reason}（登记时间 {_format_time(now)}）"
            count += 1
        if count or not skip_missing_batch:
            self._append_batch(
                entry_id,
                batch_type=batch_type,
                result="成功",
                detail=f"{reason}；本次共作废 {count} 份历史快照",
            )
        return count

    def _bump_key_version(self, entry: dict[str, Any], *, reason: str) -> None:
        entry["缓存键版本"] = int(entry.get("缓存键版本", 1)) + 1
        self._invalidate_snapshots(
            int(entry["id"]),
            reason=reason,
            batch_type=BATCH_VERSION,
        )

    def _evaluate_snapshot(self, snapshot: dict[str, Any], now: datetime) -> None:
        """读取时即时判定：到期的快照立刻标记过期，并写清判定原因。"""
        if snapshot.get("snapshot_state") == SNAPSHOT_INVALID:
            return
        expire_at = datetime.strptime(str(snapshot["expire_at"]), "%Y-%m-%d %H:%M:%S")
        generated_at = str(snapshot["generated_at"])
        ttl = int(snapshot.get("ttl_seconds", 0))
        if now > expire_at:
            # 已经登记过过期结论的快照直接沿用，避免轮询重复登记批次。
            already_expired = snapshot.get("snapshot_state") == SNAPSHOT_EXPIRED
            snapshot["snapshot_state"] = SNAPSHOT_EXPIRED
            snapshot["freshness_reason"] = (
                f"快照生成于 {generated_at}，TTL {ttl} 秒，"
                f"到期时间 {snapshot['expire_at']} 已过（当前时间 {_format_time(now)}），"
                "立即判定为过期，不再作为有效查询结果"
            )
            # 持久化到仓库，保证下一次读取仍能看到同一份过期结论。
            self._persist_state(snapshot)
            if not already_expired:
                self._append_batch(
                    int(snapshot["cache_id"]),
                    batch_type=BATCH_EXPIRE,
                    result="成功",
                    detail=(
                        f"快照 {snapshot['snapshot_no']} 于 {snapshot['expire_at']} 到期，"
                        "系统立即标记为过期，后续查询不再返回该快照"
                    ),
                    snapshot_no=str(snapshot["snapshot_no"]),
                )
        else:
            remaining = int((expire_at - now).total_seconds())
            snapshot["snapshot_state"] = SNAPSHOT_FRESH
            snapshot["freshness_reason"] = (
                f"快照生成于 {generated_at}，TTL {ttl} 秒，"
                f"到期时间 {snapshot['expire_at']} 未到，剩余 {remaining} 秒，判定为新鲜"
            )

    def _persist_state(self, snapshot: dict[str, Any]) -> None:
        for row in store.rows(SNAPSHOT_MODULE):
            if int(row.get("id", 0)) == int(snapshot.get("id", 0)):
                row["snapshot_state"] = snapshot["snapshot_state"]
                row["freshness_reason"] = snapshot["freshness_reason"]
                break
