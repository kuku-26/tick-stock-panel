// 问财实盘模拟 — 局部 API 客户端（自包含新模块，不写入核心 api.ts）
// 后端路由见 backend/app/paper/api.py (/api/paper/*)

export interface Position {
  symbol: string
  qty: number
  avg_cost: number
  entry_date: string
  hold_days: number
}

export interface Account {
  id: string
  name: string
  initial_cash: number
  cash: number
  positions: Record<string, Position>
  pending: Array<{ symbol: string; qty: number; decision_date: string }>
  enabled: boolean
  last_record_date: string | null
}

export interface FieldFilter {
  field: string
  op: string
  value: number | string
}

export type FieldType = 'number' | 'string'

export interface FieldOption {
  key: string
  label: string
  type: FieldType
}

export interface BuyRule {
  signal_ids: string[]
  iwencai_filters: FieldFilter[]
  sort_field: string
  sort_order: 'desc' | 'asc'
  top_n: number
  max_position_pct: number
  max_total_pct: number
  max_symbols: number
  buy_time: string
}

export interface SellRule {
  exit_signal_ids: string[]
  stop_loss_pct: number | null
  take_profit_pct: number | null
  max_hold_days: number | null
  sell_time: string
}

export interface PaperStrategy {
  id: string
  name: string
  account_id: string
  account_name?: string
  iwencai_query: string
  api_key: string
  enabled: boolean
  buy_rule: BuyRule
  sell_rule: SellRule
  fetch_time: string
  simulate_time: string
}

export interface ManualTrade {
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  date?: string
}

export interface SignalOption {
  id: string
  name: string
  kind: string
  column: string
}

export interface Trade {
  id: string
  account_id: string
  strategy_id: string
  date: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  amount: number
  reason: string
}

export interface DaySummary {
  date: string
  total_value: number | null
  cash: number | null
  nav: number | null
  trades: number
}

export interface AccountDay {
  date: string
  cash: number | null
  market_value: number | null
  total_value: number | null
  nav: number | null
}

export interface AccountPosition {
  symbol: string
  qty: number
  avg_cost: number
}

export interface AccountDetail {
  account: Account
  strategy_id: string | null
  strategy_name: string | null
  days: AccountDay[]
  trades: Trade[]
  positions: AccountPosition[]
}

export interface LatestSnapshot {
  date: string | null
  symbols: Array<{ symbol: string; name: string }>
  count: number
}

export interface SnapshotSheet {
  date: string | null
  rows: Array<Record<string, unknown>>
  columns: string[]
  count: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/paper${path}`, {
    headers: init?.method && init.method !== 'GET' ? { 'Content-Type': 'application/json' } : {},
    ...init,
  })
  if (!res.ok) {
    // 只读一次 body，避免 "body stream already read"
    const text = await res.text()
    let detail = ''
    try {
      detail = (JSON.parse(text) as { detail?: string }).detail ?? text
    } catch {
      detail = text || '请求失败'
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const paperApi = {
  accounts: () => req<{ accounts: Account[] }>('/accounts').then(r => r.accounts),
  createAccount: (a: { id: string; name: string; initial_cash: number }) =>
    req<{ ok: boolean }>('/accounts', { method: 'POST', body: JSON.stringify(a) }),
  deleteAccount: (id: string) => req<{ ok: boolean }>(`/accounts/${id}`, { method: 'DELETE' }),
  updateAccount: (id: string, a: { name: string; initial_cash: number }) =>
    req<{ ok: boolean }>(`/accounts/${id}`, { method: 'PUT', body: JSON.stringify(a) }),
  manualTrade: (accountId: string, t: ManualTrade) =>
    req<{ ok: boolean }>(`/accounts/${accountId}/trade`, { method: 'POST', body: JSON.stringify(t) }),

  strategies: () =>
    req<{ strategies: PaperStrategy[] }>('/strategies').then(r => r.strategies),
  options: (strategyId?: string) => {
    const q = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : ''
    return req<{ signals: SignalOption[]; fields: FieldOption[] }>(`/strategy/options${q}`)
      .then(r => ({ signals: r.signals, fields: r.fields }))
  },
  previewFields: (query: string, apiKey = '') =>
    req<{ count: number; symbols: string[]; fields: FieldOption[] }>(
      '/strategy/preview',
      { method: 'POST', body: JSON.stringify({ query, api_key: apiKey }) },
    ),
  createStrategy: (s: PaperStrategy) =>
    req<{ ok: boolean }>('/strategies', { method: 'POST', body: JSON.stringify(s) }),
  updateStrategy: (id: string, s: PaperStrategy) =>
    req<{ ok: boolean }>(`/strategies/${id}`, { method: 'PUT', body: JSON.stringify(s) }),
  deleteStrategy: (id: string) => req<{ ok: boolean }>(`/strategies/${id}`, { method: 'DELETE' }),

  accountDetail: (id: string) =>
    req<AccountDetail>(`/account/${id}/detail`),
  latest: (id: string) =>
    req<LatestSnapshot>(`/strategies/${id}/latest`),
  snapshot: (id: string, date?: string) => {
    const q = date ? `?date=${date}` : ''
    return req<SnapshotSheet>(`/strategies/${id}/snapshot${q}`)
  },

  fetchNow: (id: string) =>
    req<{ ok: boolean }>(`/strategies/${id}/fetch`, { method: 'POST' }),
  simulateNow: (id: string, date?: string, fallback?: string) => {
    const q = []
    if (date) q.push(`date=${date}`)
    if (fallback) q.push(`fallback=${encodeURIComponent(fallback)}`)
    return req<{ ok: boolean }>(
      `/strategies/${id}/simulate${q.length ? `?${q.join('&')}` : ''}`,
      { method: 'POST' },
    )
  },

  equity: (id: string) =>
    req<{ equity: Array<{ date: string; nav: number | null; total_value: number | null }> }>(
      `/strategies/${id}/equity`,
    ).then(r => r.equity),
  days: (id: string) =>
    req<{ days: DaySummary[] }>(`/strategies/${id}/days`).then(r => r.days),
  trades: (id: string) => req<{ trades: Trade[] }>(`/strategies/${id}/trades`).then(r => r.trades),
  iwencaiDates: (id: string) =>
    req<{ dates: string[] }>(`/strategies/${id}/iwencai?list=true`).then(r => r.dates),
}