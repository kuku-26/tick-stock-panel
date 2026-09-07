import { useEffect, useMemo, useState } from 'react'
import { LineChart, Wallet, Save, Trash2, RefreshCw, Play, Settings2, Loader2, Plus, Pencil, History } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { paperApi, type Account, type AccountDay, type AccountDetail, type FieldOption, type ManualTrade, type PaperStrategy, type SignalOption, type SnapshotSheet } from './api'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-secondary">
      <span>{label}</span>
      {children}
    </label>
  )
}

const inputCls =
  'h-8 rounded-btn bg-elevated px-2 text-xs text-foreground border border-border focus:border-primary outline-none'

function StrategyForm({
  signals, fields, accounts, draft, isNew, setDraft, submit, onTestFields,
}: {
  signals: SignalOption[]
  fields: FieldOption[]
  accounts: Account[]
  draft: Partial<PaperStrategy>
  isNew: boolean
  setDraft: (d: Partial<PaperStrategy>) => void
  submit: (newAccount?: { id: string; name: string; initial_cash: number } | null) => void
  onTestFields: (query: string, apiKey?: string) => Promise<FieldOption[]>
}) {
  const [sig, setSig] = useState('')
  const [fieldSel, setFieldSel] = useState('')
  const [opSel, setOpSel] = useState('>')
  const [valSel, setValSel] = useState('')
  const [newAccMode, setNewAccMode] = useState(false)
  const [newAcc, setNewAcc] = useState({ id: '', name: '', initial_cash: 100000 })
  const [dynFields, setDynFields] = useState<FieldOption[]>([])
  const [testing, setTesting] = useState(false)
  const [testErr, setTestErr] = useState('')
  const buyRule = draft.buy_rule ?? { signal_ids: [], iwencai_filters: [], sort_field: '', sort_order: 'desc', top_n: 0, max_position_pct: 0.2, max_total_pct: 0, max_symbols: 0, buy_time: 'next_open' }
  const sellRule = draft.sell_rule ?? { exit_signal_ids: [], stop_loss_pct: null, take_profit_pct: null, max_hold_days: null, sell_time: 'close' }
  // 预览/快照字段在前、内置字段兜底，去重
  const effFields: FieldOption[] = []
  for (const f of [...dynFields, ...fields]) if (!effFields.some(x => x.key === f.key)) effFields.push(f)

  const update = (u: Partial<PaperStrategy>) => {
    setDraft({ ...draft, ...u })
  }
  const addSig = (target: 'buy' | 'sell', id: string) => {
    if (!id) return
    if (target === 'buy') {
      if (!buyRule.signal_ids.includes(id))
        setDraft({ ...draft, buy_rule: { ...buyRule, signal_ids: [...buyRule.signal_ids, id] } })
    } else {
      if (!sellRule.exit_signal_ids.includes(id))
        setDraft({ ...draft, sell_rule: { ...sellRule, exit_signal_ids: [...sellRule.exit_signal_ids, id] } })
    }
    setSig('')
  }
  const addFieldCond = () => {
    if (!fieldSel || valSel.trim() === '') return
    const f = effFields.find(x => x.key === fieldSel)
    const value = f?.type === 'number' ? Number(valSel) : valSel.trim()
    if (buyRule.iwencai_filters.some(x => x.field === fieldSel && x.op === opSel && String(x.value) === String(value))) return
    setDraft({
      ...draft,
      buy_rule: { ...buyRule, iwencai_filters: [...buyRule.iwencai_filters, { field: fieldSel, op: opSel, value }] },
    })
    setFieldSel(''); setValSel(''); setOpSel('>')
  }
  const runTest = async () => {
    if (!draft.iwencai_query?.trim()) { setTestErr('请先填写问财问句'); return }
    setTesting(true); setTestErr('')
    try { setDynFields(await onTestFields(draft.iwencai_query, draft.api_key)) }
    catch (e) { setTestErr(String(e)) }
    finally { setTesting(false) }
  }

  return (
    <div className="grid grid-cols-2 gap-3">
      <Field label="策略ID">
        <input className={inputCls} value={draft.id ?? ''} disabled={!isNew}
          placeholder="如：iwen_dde_top" onChange={e => update({ id: e.target.value })} />
      </Field>
      <Field label="策略名称">
        <input className={inputCls} value={draft.name ?? ''} onChange={e => update({ name: e.target.value })} />
      </Field>
      <Field label="绑定账户">
        <select className={inputCls}
          value={newAccMode ? '__new__' : (draft.account_id ?? '')}
          onChange={e => {
            if (e.target.value === '__new__') { setNewAccMode(true); update({ account_id: undefined }) }
            else { setNewAccMode(false); update({ account_id: e.target.value }) }
          }}>
          <option value="">— 选择账户 —</option>
          {accounts.map(a => <option key={a.id} value={a.id}>{a.name} ({a.id})</option>)}
          <option value="__new__">＋ 新建账户（一并创建）</option>
        </select>
      </Field>
      {newAccMode && (
        <div className="col-span-2 rounded-btn bg-elevated/50 border border-border p-2 flex flex-col gap-2">
          <div className="text-[11px] text-secondary">账号创建后即与当前策略一对一绑定</div>
          <div className="grid grid-cols-3 gap-2">
            <Field label="账户ID">
              <input className={inputCls} value={newAcc.id} placeholder="如：demo_01" onChange={e => setNewAcc({ ...newAcc, id: e.target.value })} />
            </Field>
            <Field label="名称">
              <input className={inputCls} value={newAcc.name} onChange={e => setNewAcc({ ...newAcc, name: e.target.value })} />
            </Field>
            <Field label="初始资金">
              <input className={inputCls} type="number" min="0" value={newAcc.initial_cash}
                onChange={e => setNewAcc({ ...newAcc, initial_cash: Number(e.target.value) })} />
            </Field>
          </div>
        </div>
      )}
      <div className="col-span-2 flex flex-col gap-1.5">
        <Field label="问财选股问句（自然语言）">
          <textarea className="h-16 rounded-btn bg-elevated px-2 py-1 text-xs text-foreground border border-border"
            value={draft.iwencai_query ?? ''}
            onChange={e => update({ iwencai_query: e.target.value })} />
        </Field>
        <div className="flex items-center gap-2">
          <button type="button" onClick={runTest} disabled={testing}
            className="inline-flex items-center gap-1 h-7 px-2 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground disabled:opacity-50">
            {testing ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            {testing ? '测试中…' : '测试问财并获取字段'}
          </button>
          {dynFields.length > 0 && (
            <span className="text-[11px] text-secondary">已从问财返回 {dynFields.length} 个动态字段，可在买入条件下拉选择（实时字段优先，内置字段兜底）</span>
          )}
          {testErr && <span className="text-[11px] text-red-400">{testErr}</span>}
        </div>
      </div>
      <Field label="问财 API Key（留空用 IWENCAI_API_KEY）">
        <input className={inputCls} value={draft.api_key ?? ''} type="password"
          onChange={e => update({ api_key: e.target.value })} />
      </Field>
      <Field label="选股时间（HH:MM）">
        <input className={inputCls} value={draft.fetch_time ?? '09:25'}
          onChange={e => update({ fetch_time: e.target.value })} />
      </Field>
      <Field label="交易结算时间（HH:MM）">
        <input className={inputCls} value={draft.simulate_time ?? '15:30'}
          onChange={e => update({ simulate_time: e.target.value })} />
      </Field>

      <div className="col-span-2 rounded-btn border border-border p-3 flex flex-col gap-2">
        <div className="text-xs text-secondary font-medium">买入规则</div>
        {signals.length === 0 && (
          <div className="text-[11px] text-muted">信号盘暂无自定义信号（data/user_data/custom_signals 为空）：请先在「信号盘」创建 csg 信号，或仅配置下方问财字段条件。</div>
        )}
        <div className="flex items-center gap-2 text-xs">
          <Field label="入场信号（csg_，AND）">
            <select className={inputCls} value={sig} onChange={e => addSig('buy', e.target.value)}>
              <option value="">— 选择信号 —</option>
              {signals.map(s => <option key={s.id} value={s.id}>{s.name} ({s.kind})</option>)}
            </select>
          </Field>
          <Field label="单股资金占比上限">
            <input className={inputCls} type="number" step="0.01" min="0.01" max="1"
              value={buyRule.max_position_pct}
              onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, max_position_pct: Number(e.target.value) } })} />
          </Field>
          <Field label="单日最多买数(0不限)">
            <input className={inputCls} type="number" min="0"
              value={buyRule.max_symbols}
              onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, max_symbols: Number(e.target.value) } })} />
          </Field>
        </div>
        <div className="flex items-end gap-2 flex-wrap">
          <Field label="排序字段(留空=按问财顺序)">
            <select className={inputCls} value={buyRule.sort_field} onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, sort_field: e.target.value } })}>
              <option value="">— 不排序 —</option>
              {effFields.map(f => <option key={f.key} value={f.key}>{f.label} ({f.key})</option>)}
            </select>
          </Field>
          <Field label="排序方向">
            <select className={inputCls} value={buyRule.sort_order} onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, sort_order: e.target.value as 'desc' | 'asc' } })}>
              <option value="desc">降序（大→小）</option>
              <option value="asc">升序（小→大）</option>
            </select>
          </Field>
          <Field label="只买前N只(0不限)">
            <input className={inputCls} type="number" min="0"
              value={buyRule.top_n}
              onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, top_n: Number(e.target.value) } })} />
          </Field>
          <Field label="总仓位上限(0不限)">
            <input className={inputCls} type="number" step="0.01" min="0" max="1"
              value={buyRule.max_total_pct}
              onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, max_total_pct: Number(e.target.value) } })} />
          </Field>
          <Field label="买入时间">
            <select className={inputCls} value={buyRule.buy_time} onChange={e => setDraft({ ...draft, buy_rule: { ...buyRule, buy_time: e.target.value } })}>
              <option value="next_open">次日开盘价</option>
              <option value="same_open">当日开盘价</option>
            </select>
          </Field>
        </div>
        {buyRule.signal_ids.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {buyRule.signal_ids.map(id => (
              <span key={id} className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-0.5 text-xs">
                csg_{id}
                <button type="button" onClick={() => setDraft({
                  ...draft,
                  buy_rule: { ...buyRule, signal_ids: buyRule.signal_ids.filter(x => x !== id) },
                })}>×</button>
              </span>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 flex-wrap">
          <Field label="问财字段条件（OR）">
            <select className={inputCls} value={fieldSel} onChange={e => setFieldSel(e.target.value)}>
              <option value="">— 选择字段 —</option>
              {effFields.map(f => <option key={f.key} value={f.key}>{f.label} ({f.key})</option>)}
            </select>
          </Field>
          <Field label="操作符">
            <select className={inputCls} value={opSel} onChange={e => setOpSel(e.target.value)}>
              {(['>', '>=', '<', '<=', '==', '!=', 'contains', 'not_contains'] as const).map(op => (
                <option key={op} value={op}>{op}</option>
              ))}
            </select>
          </Field>
          <Field label="值">
            <input className={inputCls} value={valSel} placeholder={effFields.find(f => f.key === fieldSel)?.type === 'string' ? '如：主板 / ai / 中字头' : '如：0 / 10'}
              onChange={e => setValSel(e.target.value)} />
          </Field>
          <button type="button" onClick={addFieldCond} className="h-8 px-3 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground">+ 添加条件</button>
        </div>
        {buyRule.iwencai_filters.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {buyRule.iwencai_filters.map((f, i) => (
              <span key={i} className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-0.5 text-xs">
                {f.field} {f.op} {f.value}
                <button type="button" onClick={() => setDraft({
                  ...draft,
                  buy_rule: { ...buyRule, iwencai_filters: buyRule.iwencai_filters.filter((_, j) => j !== i) },
                })}>×</button>
              </span>
            ))}
          </div>
        )}
        <div className="text-[11px] text-muted">信号库条件与问财字段条件「任一来源满足即可」；两者都未配置则买入全部候选。</div>
      </div>

      <div className="col-span-2 rounded-btn border border-border p-3 flex flex-col gap-2">
        <div className="text-xs text-secondary font-medium">卖出规则（任一命中即卖出，收盘价成交）</div>
        <div className="flex items-center gap-2 text-xs">
          <Field label="离场信号（csg_，OR）">
            <select className={inputCls} value={sig} onChange={e => addSig('sell', e.target.value)}>
              <option value="">— 选择信号 —</option>
              {signals.map(s => <option key={s.id} value={s.id}>{s.name} ({s.kind})</option>)}
            </select>
          </Field>
          <Field label="止损(%)">
            <input className={inputCls} type="number" step="0.01"
              value={sellRule.stop_loss_pct ?? ''}
              onChange={e => setDraft({ ...draft, sell_rule: { ...sellRule, stop_loss_pct: e.target.value === '' ? null : Number(e.target.value) } })} />
          </Field>
          <Field label="止盈(%)">
            <input className={inputCls} type="number" step="0.01"
              value={sellRule.take_profit_pct ?? ''}
              onChange={e => setDraft({ ...draft, sell_rule: { ...sellRule, take_profit_pct: e.target.value === '' ? null : Number(e.target.value) } })} />
          </Field>
          <Field label="最长持有(日,空=不限)">
            <input className={inputCls} type="number" min="1"
              value={sellRule.max_hold_days ?? ''}
              onChange={e => setDraft({ ...draft, sell_rule: { ...sellRule, max_hold_days: e.target.value === '' ? null : Number(e.target.value) } })} />
          </Field>
        </div>
        {sellRule.exit_signal_ids.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {sellRule.exit_signal_ids.map(id => (
              <span key={id} className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-0.5 text-xs">
                csg_{id}
                <button type="button" onClick={() => setDraft({
                  ...draft,
                  sell_rule: { ...sellRule, exit_signal_ids: sellRule.exit_signal_ids.filter(x => x !== id) },
                })}>×</button>
              </span>
            ))}
          </div>
        )}
        <div className="text-[11px] text-muted">止损/止盈为小数制，如 -0.08 表示亏损 8% 止损、0.3 表示盈利 30% 止盈。</div>
      </div>

      <button type="button"
        onClick={() => submit(newAccMode ? { id: newAcc.id, name: newAcc.name, initial_cash: newAcc.initial_cash } : null)}
        className="col-span-2 inline-flex items-center justify-center gap-1 h-9 rounded-btn bg-primary text-primary-foreground text-xs hover:opacity-90">
        <Save size={14} /> 保存策略
      </button>
    </div>
  )
}

function AccountDetailPanel({ detail, onEdit, onManualTrade }: {
  detail: AccountDetail
  onEdit: () => void
  onManualTrade: () => void
}) {
  return (
    <div className="rounded-btn border border-border p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-sm font-medium flex items-center gap-1"><Wallet size={14} /> {detail.account.name} · 账户详情</span>
        <span className="flex items-center gap-1">
          <button onClick={onEdit} className="inline-flex items-center gap-1 h-6 px-2 rounded-btn bg-elevated text-[11px] text-secondary hover:text-foreground hover:bg-elevated/80">
            <Pencil size={11} /> 编辑
          </button>
          <button onClick={onManualTrade} className="inline-flex items-center gap-1 h-6 px-2 rounded-btn bg-elevated text-[11px] text-secondary hover:text-foreground hover:bg-elevated/80">
            <Plus size={11} /> 新增交易
          </button>
        </span>
      </div>
      <div className="text-[11px] text-secondary">绑定策略：{detail.strategy_name || <i className="text-muted">未绑定</i>}</div>
      <div className="rounded-btn bg-elevated/50 p-2 flex gap-4 text-[11px]">
        <span>可用资金 <b className="text-foreground">{detail.account.cash.toLocaleString()}</b></span>
        <span>持仓 {detail.positions.length} 只</span>
        <span>流水 {detail.trades.length} 笔</span>
        <span>结算 {detail.days.length} 天</span>
      </div>

      <BalanceCurve days={detail.days} />

      <div>
        <div className="text-xs font-medium text-secondary mb-1">持仓股</div>
        {detail.positions.length === 0 ? (
          <div className="text-xs text-muted">当前无持仓。</div>
        ) : (
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-secondary border-b border-border"><th className="py-1">代码</th><th>数量</th><th>成本价</th></tr>
            </thead>
            <tbody>
              {detail.positions.map(p => (
                <tr key={p.symbol} className="border-b border-border/50">
                  <td className="py-1">{p.symbol}</td><td>{p.qty}</td><td>{p.avg_cost}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <div className="text-xs font-medium text-secondary mb-1">交易流水</div>
        {detail.trades.length === 0 ? (
          <div className="text-xs text-muted">暂无成交流水，点击策略卡片「结算」或到定时结算时间后生成。</div>
        ) : (
          <div className="overflow-auto max-h-64">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-secondary border-b border-border">
                  <th className="py-1">日期</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>金额</th><th>原因</th>
                </tr>
              </thead>
              <tbody>
                {detail.trades.slice().reverse().map((t, i) => (
                  <tr key={i} className="border-b border-border/50">
                    <td className="py-1">{t.date}</td>
                    <td>{t.symbol}</td>
                    <td className={t.side === 'buy' ? 'text-red-300' : 'text-emerald-300'}>{t.side === 'buy' ? '买入' : '卖出'}</td>
                    <td>{t.qty}</td><td>{t.price}</td>
                    <td>{t.amount?.toLocaleString()}</td>
                    <td className="text-muted">{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function SnapshotTable({ data }: { data: SnapshotSheet | null }) {
  const [sort, setSort] = useState<{ col: string; dir: 'asc' | 'desc' } | null>(null)
  useEffect(() => { setSort(null) }, [data])
  const rows = useMemo(() => {
    if (!data || !sort) return data?.rows ?? []
    const arr = data.rows.slice()
    const num = (v: unknown) => { const n = Number(v); return Number.isFinite(n) ? n : NaN }
    arr.sort((a, b) => {
      const va = num(a[sort.col]); const vb = num(b[sort.col])
      const useNum = Number.isFinite(va) && Number.isFinite(vb)
      const c = useNum ? va - vb
        : String(a[sort.col] ?? '').localeCompare(String(b[sort.col] ?? ''), 'zh')
      return sort.dir === 'asc' ? c : -c
    })
    return arr
  }, [data, sort])
  const toggleSort = (c: string) =>
    setSort(s => s?.col === c ? { col: c, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { col: c, dir: 'asc' })
  if (!data || data.rows.length === 0) {
    return <div className="text-xs text-muted">该日期暂无问财快照数据。</div>
  }
  return (
    <div className="overflow-auto">
      <div className="text-[11px] text-secondary mb-1">点击列头可按该字段排序（数字/文本）</div>
      <table className="w-max min-w-full text-left text-xs">
        <thead>
          <tr className="text-secondary border-b border-border">
            {data.columns.map(c => (
              <th key={c} onClick={() => toggleSort(c)}
                className="py-1 px-2 whitespace-nowrap cursor-pointer select-none hover:text-foreground">
                {c}{sort?.col === c ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : ''}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border/50">
              {data.columns.map(c => <td key={c} className="py-1 px-2 whitespace-nowrap">{String(r[c] ?? '')}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SnapshotSheetModal({ strategyId, name, onClose }: {
  strategyId: string
  name: string
  onClose: () => void
}) {
  const [data, setData] = useState<SnapshotSheet | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let alive = true
    paperApi.snapshot(strategyId)
      .then(d => alive && setData(d))
      .catch(e => alive && setErr(String(e)))
    return () => { alive = false }
  }, [strategyId])
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-6 overflow-auto" onClick={onClose}>
      <div className="rounded-btn bg-elevated border border-border p-4 flex flex-col gap-3 w-full max-w-5xl max-h-[85vh] overflow-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">{name || strategyId} · 当日选股名单{data?.date ? `（${data.date}）` : ''}{data ? ` · ${data.count} 只` : ''}</span>
          <button onClick={onClose} className="text-xs text-secondary hover:text-foreground">× 关闭</button>
        </div>
        {err && <div className="text-xs text-red-400">{err}</div>}
        {!data && !err && <div className="text-xs text-muted">加载中…</div>}
        {data && data.rows.length === 0 && (
          <div className="text-xs text-muted">暂无问财快照，或尚未到定时选股时间。可点击「拉取」手动获取一次。</div>
        )}
        {data && data.rows.length > 0 && <SnapshotTable data={data} />}
      </div>
    </div>
  )
}

function HistoryPanelModal({ strategies, initialStrategyId, onClose }: {
  strategies: PaperStrategy[]
  initialStrategyId?: string
  onClose: () => void
}) {
  const [sid, setSid] = useState(initialStrategyId ?? strategies[0]?.id ?? '')
  const [dates, setDates] = useState<string[]>([])
  const [date, setDate] = useState('')
  const [data, setData] = useState<SnapshotSheet | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  // 切换策略 → 拉取该策略的落盘日期列表，默认选最新一天
  useEffect(() => {
    let alive = true
    setDates([]); setDate(''); setData(null); setErr('')
    if (!sid) return
    paperApi.iwencaiDates(sid)
      .then(ds => {
        if (!alive) return
        setDates(ds)
        if (ds.length) setDate(ds[ds.length - 1])
        else setErr('该策略暂无落盘的选股快照')
      })
      .catch(e => alive && setErr(String(e)))
    return () => { alive = false }
  }, [sid])

  // 切换日期 → 拉取当日选股明细
  useEffect(() => {
    let alive = true
    if (!sid || !date) { setData(null); return }
    setData(null); setLoading(true); setErr('')
    paperApi.snapshot(sid, date)
      .then(d => alive && setData(d))
      .catch(e => alive && setErr(String(e)))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [sid, date])

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-6 overflow-auto" onClick={onClose}>
      <div className="rounded-btn bg-elevated border border-border p-4 flex flex-col gap-3 w-full max-w-5xl max-h-[85vh] overflow-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">选股历史 · 按日期查看落盘的选股数据</span>
          <button onClick={onClose} className="text-xs text-secondary hover:text-foreground">× 关闭</button>
        </div>

        <div className="flex flex-wrap items-center gap-1">
          {strategies.map(s => (
            <button key={s.id} onClick={() => setSid(s.id)}
              className={`h-7 px-3 rounded-btn text-xs border ${s.id === sid
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-elevated text-secondary border-border hover:text-foreground'}`}>
              {s.name}
            </button>
          ))}
          {strategies.length === 0 && <span className="text-xs text-muted">暂无策略</span>}
        </div>

        <div className="flex items-center gap-2 text-xs text-secondary">
          <span>日期</span>
          <select className={inputCls} value={date} onChange={e => setDate(e.target.value)}
            disabled={!dates.length}>
            {!dates.length && <option value="">— 无快照 —</option>}
            {dates.slice().reverse().map(d => <option key={d} value={d}>{d}</option>)}
          </select>
          {data && <span className="text-muted">{data.count} 只</span>}
          {loading && <span className="text-muted">加载中…</span>}
        </div>

        {err && <div className="text-xs text-red-400">{err}</div>}
        {!loading && data && data.rows.length === 0 && (
          <div className="text-xs text-muted">{date} 当日快照为空（问财未返回结果）。</div>
        )}
        {!loading && data && data.rows.length > 0 && <SnapshotTable data={data} />}
      </div>
    </div>
  )
}

function StrategyCard({ s, onOpen, onPatch, onFetch, onSimulate, onDelete, onEditAccount, onManualTrade }: {
  s: PaperStrategy
  onOpen: () => void
  onPatch: (patch: Partial<PaperStrategy>) => void
  onFetch: () => void
  onSimulate: () => void
  onDelete: () => void
  onEditAccount: (accountId: string) => void
  onManualTrade: (account: Account) => void
}) {
  const [detail, setDetail] = useState<AccountDetail | null>(null)
  const [showSheet, setShowSheet] = useState(false)
  useEffect(() => {
    let alive = true
    setDetail(null)
    if (s.account_id) {
      paperApi.accountDetail(s.account_id).then(d => alive && setDetail(d)).catch(() => alive && setDetail(null))
    }
    return () => { alive = false }
  }, [s.id, s.account_id])

  return (
    <div className="rounded-btn border border-border p-4 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <button onClick={onOpen} className="text-base font-medium text-foreground hover:text-primary text-left">
          {s.name}
        </button>
        <label className="flex items-center gap-1 text-[11px] text-secondary">
          <input type="checkbox" checked={s.enabled} onChange={e => onPatch({ enabled: e.target.checked })} />
          启用
        </label>
      </div>
      <div className="text-xs text-muted">问句：{s.iwencai_query}</div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-secondary">
        <span>账户：{s.account_name || s.account_id}</span>
        <span className="flex items-center gap-1">
          选股
          <input type="time" className={`${inputCls} w-[92px]`} value={s.fetch_time}
            onChange={e => onPatch({ fetch_time: e.target.value })} />
        </span>
        <span className="flex items-center gap-1">
          结算
          <input type="time" className={`${inputCls} w-[92px]`} value={s.simulate_time}
            onChange={e => onPatch({ simulate_time: e.target.value })} />
        </span>
      </div>
      <div className="flex items-center gap-1 border-t border-border pt-2">
        <button onClick={onOpen} className="inline-flex items-center gap-1 h-7 px-2 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground hover:bg-elevated/80">
          <Settings2 size={12} /> 编辑
        </button>
        <button onClick={() => setShowSheet(true)} className="inline-flex items-center gap-1 h-7 px-2 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground hover:bg-elevated/80">
          <LineChart size={12} /> 选股名单
        </button>
        <button onClick={onFetch} className="inline-flex items-center gap-1 h-7 px-2 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground hover:bg-elevated/80">
          <RefreshCw size={12} /> 拉取
        </button>
        <button onClick={onSimulate} className="inline-flex items-center gap-1 h-7 px-2 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground hover:bg-elevated/80">
          <Play size={12} /> 结算
        </button>
        <button onClick={onDelete} className="ml-auto inline-flex items-center gap-1 h-7 px-2 rounded-btn text-xs text-red-400 hover:bg-red-500/10">
          <Trash2 size={12} />
        </button>
      </div>

      {detail && (
        <AccountDetailPanel detail={detail}
          onEdit={() => onEditAccount(detail.account.id)}
          onManualTrade={() => onManualTrade(detail.account)} />
      )}
      {showSheet && (
        <SnapshotSheetModal strategyId={s.id} name={s.name} onClose={() => setShowSheet(false)} />
      )}
    </div>
  )
}

function BalanceCurve({ days }: { days: AccountDay[] }) {
  const toNum = (v: number | null | undefined) => (typeof v === 'number' ? v : null)
  const cash = days.map(d => toNum(d.cash))
  const tot = days.map(d => toNum(d.total_value))
  const allN = [...cash, ...tot].filter((v): v is number => v !== null)
  if (days.length === 0 || allN.length === 0) {
    return <div className="text-xs text-muted">暂无结算数据，无法绘制余额曲线。</div>
  }
  const pad = 6, w = 340, h = 90
  const min = Math.min(...allN), max = Math.max(...allN)
  const span = (max - min) || 1
  const n = days.length
  const x = (i: number) => pad + (i * (w - pad * 2) / (n === 1 ? 1 : n - 1))
  const y = (v: number) => h - pad - ((v - min) / span) * (h - pad * 2)
  const pts = (vals: (number | null)[]) => vals
    .map((v, i) => (v === null ? null : `${x(i)},${y(v)}`)).filter(Boolean)!.join(' ')
  const lastCash = cash[cash.length - 1]
  const lastTot = tot[tot.length - 1]
  return (
    <div className="flex flex-col gap-1">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-24" preserveAspectRatio="none">
        <polyline points={pts(cash)} fill="none" stroke="#f87171" strokeWidth="1.5" />
        <polyline points={pts(tot)} fill="none" stroke="#60a5fa" strokeWidth="1.5" />
      </svg>
      <div className="flex gap-4 text-[11px] text-secondary">
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 bg-red-400 inline-block" /> 可用资金 {lastCash === null ? '-' : lastCash.toLocaleString()}</span>
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 bg-blue-400 inline-block" /> 总资产 {lastTot === null ? '-' : lastTot.toLocaleString()}</span>
      </div>
    </div>
  )
}

function PaperPage() {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [strategies, setStrategies] = useState<PaperStrategy[]>([])
  const [signals, setSignals] = useState<SignalOption[]>([])
  const [fields, setFields] = useState<FieldOption[]>([])
  const [formFields, setFormFields] = useState<FieldOption[] | null>(null)
  const [editing, setEditing] = useState<Partial<PaperStrategy> | null>(null)
  const [msg, setMsg] = useState('')
  const [accEditing, setAccEditing] = useState<Account | null>(null)
  const [accEditDraft, setAccEditDraft] = useState<{ name: string; initial_cash: number } | null>(null)
  const [manualAcc, setManualAcc] = useState<Account | null>(null)
  const [manualDraft, setManualDraft] = useState<ManualTrade>({ symbol: '', side: 'buy', qty: 100, price: 0, date: '' })
  const [showHistory, setShowHistory] = useState(false)

  const load = async () => {
    const [a, s, opt] = await Promise.all([paperApi.accounts(), paperApi.strategies(), paperApi.options()])
    setAccounts(a); setStrategies(s); setSignals(opt.signals); setFields(opt.fields)
  }
  useEffect(() => {
    load().catch(e => setMsg(String(e)))
  }, [])

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(''), 4000) }

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); await load(); flash(ok) } catch (e) { flash(String(e)) }
  }

  // 打开策略编辑器：新策略仅用静态字段；编辑已存策略时并入其最近快照字段（快照回退）
  const openEditor = async (s: PaperStrategy | null) => {
    setEditing(s ? { ...s } : {})
    if (s) {
      try { const opt = await paperApi.options(s.id); setFormFields(opt.fields) }
      catch { setFormFields(null) }
    } else setFormFields(null)
  }
  // 「测试问财并获取字段」回调：实时调问财获取动态字段（合并交给表单内部做）
  const testFields = async (query: string, apiKey?: string): Promise<FieldOption[]> => {
    const r = await paperApi.previewFields(query, apiKey)
    return r.fields
  }

  const closeEditor = () => setEditing(null)
  const openCreate = () => { closeEditor(); openEditor(null) }
  // 打开策略编辑面板（配置编辑）；账户详情已内联在卡片上，此处仅编辑规则
  const openEdit = async (s: PaperStrategy) => {
    await openEditor(s)
  }

  const saveStrategy = async (newAccount?: { id: string; name: string; initial_cash: number } | null) => {
    if (!editing?.id || !editing?.name || !editing?.iwencai_query) {
      flash('请完整填写策略ID、名称与问句'); return
    }
    let accountId = editing.account_id || ''
    try {
      // 一并创建账户（账户+策略一起创建，而不是先建账户再绑定）
      if (newAccount) {
        if (!newAccount.id || !newAccount.name) { flash('请完整填写新账户ID与名称'); return }
        accountId = newAccount.id
        await paperApi.createAccount({ id: newAccount.id, name: newAccount.name, initial_cash: newAccount.initial_cash })
      }
      if (!accountId) { flash('请选择或新建一个账户'); return }
      const payload = { ...editing, account_id: accountId }
      if (strategies.some(s => s.id === editing.id)) await paperApi.updateStrategy(editing.id, payload as PaperStrategy)
      else await paperApi.createStrategy(payload as PaperStrategy)
      setEditing(null); await load(); flash('策略已保存')
    } catch (e) { flash(String(e)) }
  }

  // 卡片上的定时执行/启用设置：本地即时更新并持久化
  const onPatchStrategy = async (id: string, patch: Partial<PaperStrategy>) => {
    const idx = strategies.findIndex(x => x.id === id)
    if (idx < 0) return
    const next = { ...strategies[idx], ...patch }
    setStrategies(strategies.map((x, i) => i === idx ? next : x))
    try { await paperApi.updateStrategy(id, next as PaperStrategy) }
    catch (e) { flash(String(e)); await load() }
  }

  const removeStrategy = async (s: PaperStrategy) => {
    if (!window.confirm(`确定删除策略卡片「${s.name}」？将同时删除其绑定的账户及全部落盘数据（问财快照/日快照/成交记录）。`)) return
    try {
      await paperApi.deleteStrategy(s.id)
      if (s.account_id) { try { await paperApi.deleteAccount(s.account_id) } catch { /* 账户可能已被删，忽略 */ } }
      if (editing?.id === s.id) closeEditor()
      await load(); flash('策略及其账户、落盘数据已删除')
    } catch (e) { flash(String(e)) }
  }

  const isNew = editing ? !(strategies.some(x => x.id === editing.id)) : false

  const openAccountEdit = (accountId: string) => {
    const acc = accounts.find(a => a.id === accountId)
    if (!acc) return
    setAccEditing(acc); setAccEditDraft({ name: acc.name, initial_cash: acc.initial_cash })
  }
  const submitAccountEdit = async () => {
    if (!accEditing || !accEditDraft) return
    try {
      await paperApi.updateAccount(accEditing.id, { name: accEditDraft.name, initial_cash: accEditDraft.initial_cash })
      setAccEditing(null); await load(); flash('账户已更新')
    } catch (e) { flash(String(e)) }
  }
  const openManualTrade = (acc: Account) => {
    setManualAcc(acc); setManualDraft({ symbol: '', side: 'buy', qty: 100, price: 0, date: '' })
  }
  const submitManualTrade = async () => {
    if (!manualAcc) return
    try {
      await paperApi.manualTrade(manualAcc.id, manualDraft)
      setManualAcc(null); await load(); flash('已记入一笔手动交易')
    } catch (e) { flash(String(e)) }
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-medium text-foreground flex items-center gap-2">
          <Wallet size={18} /> 问财实盘模拟
        </h1>
        <span className="flex items-center gap-2">
          <button onClick={() => setShowHistory(true)} disabled={!strategies.length}
            className="inline-flex items-center gap-1 h-8 px-3 rounded-btn bg-elevated text-xs text-secondary hover:text-foreground disabled:opacity-50">
            <History size={13} /> 选股历史
          </button>
          <button onClick={openCreate} className="inline-flex items-center gap-1 h-8 px-3 rounded-btn bg-primary text-primary-foreground text-xs hover:opacity-90">
            <Save size={13} /> 新建策略
          </button>
        </span>
      </div>

      {msg && <div className="rounded-btn bg-amber-500/10 text-amber-300 text-xs px-3 py-2">{msg}</div>}

      {showHistory && (
        <HistoryPanelModal strategies={strategies} onClose={() => setShowHistory(false)} />
      )}

      <div className="flex flex-col gap-3">
        {strategies.map(s => (
          <StrategyCard key={s.id} s={s}
            onOpen={() => openEdit(s)}
            onPatch={patch => onPatchStrategy(s.id, patch)}
            onFetch={() => act(() => paperApi.fetchNow(s.id), `已拉取选股：${s.name}`)}
            onSimulate={() => act(() => paperApi.simulateNow(s.id), `已结算：${s.name}`)}
            onDelete={() => removeStrategy(s)}
            onEditAccount={openAccountEdit}
            onManualTrade={openManualTrade} />
        ))}
      </div>

      {editing && (
        <div className="rounded-btn border border-border p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium flex items-center gap-1"><Settings2 size={14} /> {isNew ? '新建策略' : '编辑策略'}</span>
            <button onClick={closeEditor} className="text-xs text-secondary hover:text-foreground">× 关闭</button>
          </div>
          <StrategyForm signals={signals} fields={formFields ?? fields} accounts={accounts} draft={editing}
            isNew={isNew} setDraft={setEditing} submit={saveStrategy} onTestFields={testFields} />
        </div>
      )}

      {/* 账户编辑弹窗 */}
      {accEditing && accEditDraft && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setAccEditing(null)}>
          <div className="rounded-btn bg-elevated border border-border p-4 flex flex-col gap-3 w-full max-w-sm" onClick={e => e.stopPropagation()}>
            <div className="text-sm font-medium">编辑账户 · {accEditing.id}</div>
            <Field label="账户名称">
              <input className={inputCls} value={accEditDraft.name}
                onChange={e => setAccEditDraft({ ...accEditDraft, name: e.target.value })} />
            </Field>
            <Field label="初始资金">
              <input className={inputCls} type="number" min="0"
                value={accEditDraft.initial_cash}
                onChange={e => setAccEditDraft({ ...accEditDraft, initial_cash: Number(e.target.value) })} />
            </Field>
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={() => setAccEditing(null)} className="h-7 px-3 rounded-btn text-xs bg-elevated text-secondary">取消</button>
              <button onClick={submitAccountEdit} className="h-7 px-3 rounded-btn text-xs bg-primary text-primary-foreground">保存</button>
            </div>
          </div>
        </div>
      )}

      {/* 手动新增交易弹窗 */}
      {manualAcc && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setManualAcc(null)}>
          <div className="rounded-btn bg-elevated border border-border p-4 flex flex-col gap-3 w-full max-w-sm" onClick={e => e.stopPropagation()}>
            <div className="text-sm font-medium">手动新增交易 · {manualAcc.name}</div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="股票代码">
                <input className={inputCls} placeholder="如 600000.SH" value={manualDraft.symbol}
                  onChange={e => setManualDraft({ ...manualDraft, symbol: e.target.value })} />
              </Field>
              <Field label="方向">
                <select className={inputCls} value={manualDraft.side}
                  onChange={e => setManualDraft({ ...manualDraft, side: e.target.value as 'buy' | 'sell' })}>
                  <option value="buy">买入</option><option value="sell">卖出</option>
                </select>
              </Field>
              <Field label="数量(100股整手)">
                <input className={inputCls} type="number" min="100" step="100" value={manualDraft.qty}
                  onChange={e => setManualDraft({ ...manualDraft, qty: Number(e.target.value) })} />
              </Field>
              <Field label="价格">
                <input className={inputCls} type="number" min="0" step="0.01" value={manualDraft.price}
                  onChange={e => setManualDraft({ ...manualDraft, price: Number(e.target.value) })} />
              </Field>
            </div>
            <Field label="日期(留空=今天)">
              <input className={inputCls} type="date" value={manualDraft.date ?? ''}
                onChange={e => setManualDraft({ ...manualDraft, date: e.target.value })} />
            </Field>
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={() => setManualAcc(null)} className="h-7 px-3 rounded-btn text-xs bg-elevated text-secondary">取消</button>
              <button onClick={submitManualTrade} className="h-7 px-3 rounded-btn text-xs bg-primary text-primary-foreground">提交</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const extension: FrontendExtension = {
  id: 'paper.trading',
  apiVersion: 1,
  routes: [{ id: 'paper-trading', path: '/paper', component: PaperPage }],
  navigation: [
    { id: 'paper-trading', routeId: 'paper-trading', label: '问财实盘模拟', icon: LineChart, order: 800 },
  ],
}

export default extension