"""结果缓存加速接口：维护缓存配置，覆盖启用缓存、暂停缓存、关闭缓存等动作。

同时提供缓存预热与刷新批次视图：按缓存配置查看预热/刷新批次、查询结果快照、
最近命中、过期时间、来源服务版本以及新鲜/过期判定原因。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cache import CacheService

router = APIRouter(prefix="/api/cache", tags=["结果缓存加速"])

service = CacheService()

LIST_FIELDS = ["配置编号", "缓存键名", "过期秒数", "刷新策略", "预热开关", "容量上限", "淘汰算法", "来源服务", "缓存键版本", "配置状态"]
STATUSES = ["草稿", "生效中", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤结果缓存加速列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条缓存配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="缓存配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条缓存配置执行启用缓存、暂停缓存、关闭缓存；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/refresh-policy", response_model=ActionResult)
def change_refresh_policy(entry_id: int, payload: EntryPayload) -> ActionResult:
    """变更刷新策略：策略口径变化后键版本推进，旧版本快照立即失效、不复用。"""
    policy = str(payload.values.get("刷新策略") or "").strip()
    entry, message = service.change_refresh_policy(entry_id, policy)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/batches", response_model=dict)
def list_batches(entry_id: int) -> dict[str, Any]:
    """预热与刷新批次视图：按时间倒序返回批次，最近一次生成排在最前。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"缓存配置 {entry_id} 不存在或已归档")
    batches = service.list_batches(entry_id)
    return {
        "cache_id": entry_id,
        "total": len(batches),
        "latest": batches[0] if batches else None,
        "items": batches,
    }


@router.get("/{entry_id}/snapshots", response_model=dict)
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """按缓存键名查看快照：最近命中、过期时间、来源服务版本与新鲜/过期判定原因。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"缓存配置 {entry_id} 不存在或已归档")
    snapshots = service.list_snapshots(entry_id)
    active = next((item for item in snapshots if item.get("snapshot_state") == "有效"), None)
    return {
        "cache_id": entry_id,
        "total": len(snapshots),
        "active": active,
        "items": snapshots,
    }


@router.post("/{entry_id}/prewarm", response_model=ActionResult)
def prewarm(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """执行预热批次：可重复执行，但同一缓存键名始终只保留一份有效快照。"""
    values = payload.values if payload is not None else {}
    ttl_override = _parse_ttl(values.get("过期秒数"))
    fail = bool(values.get("fail") or values.get("模拟失败"))
    snapshot, message = service.prewarm(entry_id, ttl_seconds=ttl_override, fail=fail)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.post("/{entry_id}/refresh", response_model=ActionResult)
def refresh(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """执行刷新批次：刷新失败时旧快照维持原状态，绝不标记为成功。"""
    values = payload.values if payload is not None else {}
    ttl_override = _parse_ttl(values.get("过期秒数"))
    fail = bool(values.get("fail") or values.get("模拟失败"))
    snapshot, message = service.refresh(entry_id, ttl_seconds=ttl_override, fail=fail)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.post("/{entry_id}/hit", response_model=ActionResult)
def simulate_hit(entry_id: int) -> ActionResult:
    """模拟一次业务查询：命中当前有效快照并回写最近命中时间与命中次数。"""
    snapshot, message = service.simulate_hit(entry_id)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出结果缓存加速清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


def _parse_ttl(raw: Any) -> int | None:
    """批次接口允许临时覆盖过期秒数（例如用 1 秒验证过期口径），缺省时走配置值。"""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="过期秒数必须是整数")
