import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/cache'
const columns = [
  "配置编号",
  "缓存键名",
  "过期秒数",
  "刷新策略",
  "预热开关",
  "容量上限",
  "淘汰算法",
  "来源服务",
  "缓存键版本",
  "配置状态"
]
const actions = [
  "启用缓存",
  "暂停缓存",
  "关闭缓存"
]
const stats = [
  {
    "label": "缓存配置总数",
    "value": 0
  },
  {
    "label": "生效中缓存",
    "value": 0
  },
  {
    "label": "已暂停缓存",
    "value": 0
  }
]
const POLICY_OPTIONS = ["TTL 定时刷新", "主动刷新+灰度校验", "被动失效", "异步双写刷新"]

type Row = Record<string, string | number | null>

type Snapshot = {
  id: number
  snapshot_no: string
  cache_key: string
  key_version: number
  source_version: string
  trigger: string
  ttl_seconds: number
  generated_at: string
  expire_at: string
  last_hit_at: string | null
  hit_count: number
  payload: Record<string, unknown>
  snapshot_state: string
  freshness_reason: string
}

type Batch = {
  id: number
  batch_no: string
  batch_type: string
  result: string
  snapshot_no: string | null
  detail: string
  generated_at: string
}

type BatchView = { total: number; latest: Batch | null; items: Batch[] }
type SnapshotView = { total: number; active: Snapshot | null; items: Snapshot[] }

function stateClass(state: string): string {
  if (state === '有效') return 'state-badge fresh'
  if (state === '已过期') return 'state-badge expired'
  return 'state-badge invalid'
}

function resultClass(result: string): string {
  return result === '成功' || result === '命中' ? 'state-badge fresh' : result === '失败' || result === '未命中' ? 'state-badge invalid' : 'state-badge expired'
}

export default function CacheView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [snapshotView, setSnapshotView] = useState<SnapshotView | null>(null)
  const [batchView, setBatchView] = useState<BatchView | null>(null)
  const [ttlInput, setTtlInput] = useState('')
  const [policyInput, setPolicyInput] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('缓存配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存加速列表读取失败')
    }
  }, [filters])

  const reloadDetail = useCallback(async (entryId: number) => {
    try {
      const [snapshotResponse, batchResponse] = await Promise.all([
        request(`${ENDPOINT}/${entryId}/snapshots`),
        request(`${ENDPOINT}/${entryId}/batches`),
      ])
      if (!snapshotResponse.ok || !batchResponse.ok) {
        throw new Error('缓存预热与刷新批次视图读取失败')
      }
      setSnapshotView(await snapshotResponse.json())
      setBatchView(await batchResponse.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批次视图读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (selectedId === null) {
      setSnapshotView(null)
      setBatchView(null)
      return
    }
    void reloadDetail(selectedId)
  }, [selectedId, reloadDetail])

  const selected = rows.find((row) => Number(row.id) === selectedId) ?? null

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '结果缓存加速动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message)
      await reload()
      if (selectedId !== null) await reloadDetail(selectedId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存加速操作失败')
    }
  }

  async function postAction(path: string, body: Record<string, unknown>, successText: string) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values: body }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '操作未生效')
      }
      setInfoMessage(payload.message || successText)
      await reload()
      if (selectedId !== null) await reloadDetail(selectedId)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '操作失败')
    }
  }

  async function changePolicy() {
    if (selectedId === null) return
    if (!policyInput.trim()) {
      setErrorMessage('请先选择新的刷新策略')
      return
    }
    await postAction(`${ENDPOINT}/${selectedId}/refresh-policy`, { 刷新策略: policyInput }, '刷新策略已变更')
    setPolicyInput('')
  }

  const prewarmBody = ttlInput.trim() ? { 过期秒数: ttlInput.trim() } : {}

  return (
    <section className="page" data-module="cache">
      <header className="page-head">
        <div>
          <h2>结果缓存加速管理</h2>
          <p className="page-desc">维护查询结果的缓存参数、过期策略与预热机制，并按缓存键名查看预热/刷新批次、查询结果快照与新鲜度判定。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('缓存配置登记入口尚未接入审批流')}>
            登记缓存配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出结果缓存加速清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>选择</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={Number(row.id) === selectedId ? 'row-selected' : ''}>
              <td>
                <input
                  type="radio"
                  name="cache-selected"
                  checked={Number(row.id) === selectedId}
                  onChange={() => setSelectedId(Number(row.id))}
                />
              </td>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无结果缓存加速数据，可先登记缓存配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条结果缓存加速记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
        {infoMessage && <span className="ok-text">{infoMessage}</span>}
      </footer>

      {selected && (
        <div className="detail-panel">
          <header className="detail-head">
            <div>
              <h3>缓存预热与刷新批次视图</h3>
              <p className="page-desc">
                当前配置：{String(selected['配置编号'])} ｜ 缓存键名 {String(selected['缓存键名'])} ｜
                过期秒数 {String(selected['过期秒数'])} ｜ 刷新策略 {String(selected['刷新策略'])} ｜
                缓存键版本 v{String(selected['缓存键版本'])} ｜ 来源服务 {String(selected['来源服务'])}
              </p>
            </div>
            <button className="btn ghost" type="button" onClick={() => selectedId !== null && void reloadDetail(selectedId)}>
              刷新视图
            </button>
          </header>

          <div className="action-grid">
            <div className="action-box">
              <strong>预热批次（可重复执行，仅保留一份有效结果）</strong>
              <label className="filter-item">
                <span>临时过期秒数（留空取配置值，验证过期口径可填 1）</span>
                <input value={ttlInput} placeholder="例如 1" onChange={(event) => setTtlInput(event.target.value)} />
              </label>
              <div className="row-actions">
                <button className="btn primary" type="button" onClick={() => void postAction(`${ENDPOINT}/${selectedId}/prewarm`, prewarmBody, '预热成功')}>
                  执行预热
                </button>
                <button className="btn" type="button" onClick={() => void postAction(`${ENDPOINT}/${selectedId}/prewarm`, { ...prewarmBody, 模拟失败: true }, '预热失败')}>
                  模拟预热失败
                </button>
              </div>
            </div>
            <div className="action-box">
              <strong>刷新批次（失败不会把旧快照标成成功）</strong>
              <p className="hint-text">刷新沿用当前过期秒数与来源服务版本，成功后顶替旧快照。</p>
              <div className="row-actions">
                <button className="btn primary" type="button" onClick={() => void postAction(`${ENDPOINT}/${selectedId}/refresh`, {}, '刷新成功')}>
                  执行刷新
                </button>
                <button className="btn" type="button" onClick={() => void postAction(`${ENDPOINT}/${selectedId}/refresh`, { 模拟失败: true }, '刷新失败')}>
                  模拟刷新失败
                </button>
                <button className="btn" type="button" onClick={() => void postAction(`${ENDPOINT}/${selectedId}/hit`, {}, '模拟命中')}>
                  模拟查询命中
                </button>
              </div>
            </div>
            <div className="action-box">
              <strong>刷新策略变更（键版本推进，不复用旧版本）</strong>
              <label className="filter-item">
                <span>新刷新策略</span>
                <select value={policyInput} onChange={(event) => setPolicyInput(event.target.value)}>
                  <option value="">请选择刷新策略</option>
                  {POLICY_OPTIONS.filter((item) => item !== selected['刷新策略']).map((item) => (
                    <option key={item} value={item}>{item}</option>
                  ))}
                </select>
              </label>
              <div className="row-actions">
                <button className="btn primary" type="button" onClick={() => void changePolicy()}>变更刷新策略</button>
              </div>
            </div>
          </div>

          <h4>查询结果快照（按缓存键名，最近生成在前）</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>快照编号</th>
                <th>状态</th>
                <th>键版本</th>
                <th>来源服务版本</th>
                <th>生成时间</th>
                <th>过期时间</th>
                <th>最近命中 / 次数</th>
                <th>当前字段值</th>
                <th>新鲜/过期判定原因</th>
              </tr>
            </thead>
            <tbody>
              {(snapshotView?.items ?? []).map((snapshot) => (
                <tr key={snapshot.id}>
                  <td>{snapshot.snapshot_no}<br /><span className="hint-text">触发：{snapshot.trigger} ｜ TTL {snapshot.ttl_seconds}s</span></td>
                  <td><span className={stateClass(snapshot.snapshot_state)}>{snapshot.snapshot_state}</span></td>
                  <td>v{snapshot.key_version}</td>
                  <td>{snapshot.source_version}</td>
                  <td>{snapshot.generated_at}</td>
                  <td>{snapshot.expire_at}</td>
                  <td>{snapshot.last_hit_at ?? '尚未命中'}<br /><span className="hint-text">命中 {snapshot.hit_count} 次</span></td>
                  <td><pre className="payload-pre">{JSON.stringify(snapshot.payload, null, 2)}</pre></td>
                  <td className="reason-cell">{snapshot.freshness_reason}</td>
                </tr>
              ))}
              {(snapshotView?.items.length ?? 0) === 0 && (
                <tr><td className="empty-state" colSpan={9}>该缓存键名暂无快照，先执行一次预热批次</td></tr>
              )}
            </tbody>
          </table>

          <h4>预热与刷新批次（按时间倒序，最近一次生成在最前）</h4>
          {batchView?.latest && (
            <p className="hint-text">
              最近一次生成：{batchView.latest.batch_no} ｜ {batchView.latest.batch_type} ｜
              结果 <span className={resultClass(batchView.latest.result)}>{batchView.latest.result}</span> ｜
              时间 {batchView.latest.generated_at}
            </p>
          )}
          <table className="data-table">
            <thead>
              <tr>
                <th>批次编号</th>
                <th>批次类型</th>
                <th>结果</th>
                <th>关联快照</th>
                <th>生成时间</th>
                <th>批次说明</th>
              </tr>
            </thead>
            <tbody>
              {(batchView?.items ?? []).map((batch) => (
                <tr key={batch.id}>
                  <td>{batch.batch_no}</td>
                  <td>{batch.batch_type}</td>
                  <td><span className={resultClass(batch.result)}>{batch.result}</span></td>
                  <td>{batch.snapshot_no ?? '—'}</td>
                  <td>{batch.generated_at}</td>
                  <td className="reason-cell">{batch.detail}</td>
                </tr>
              ))}
              {(batchView?.items.length ?? 0) === 0 && (
                <tr><td className="empty-state" colSpan={6}>暂无批次记录</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
