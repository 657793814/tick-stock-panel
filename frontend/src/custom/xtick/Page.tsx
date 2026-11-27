/**
 * XTick 专用能力页 —— 竞价 / 连板 / 情绪 / 资金流 / 龙虎榜 / 新闻 / K线财务。
 *
 * 归属: 扩展域消费页 (官方), 经 custom/xtick/extension.tsx 注册路由与导航。
 * 数据来自 /api/xtick/data/{kind}, 由 services.xtick 做降级与分级缓存。
 * 上游 token 走 HTTP query string, 页面明确提示这一安全边界。
 */
import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { AlertTriangle, Loader2 } from 'lucide-react'

import { api, type XTickData, type XTickPayload, type XTickRow, type XTickStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { SourceTag } from '@/components/SourceTag'
import { cn } from '@/lib/cn'
import { alignOf, formatFieldValue, resolveColumns, type FieldSpec } from '@/lib/field-labels'
import { priceColorClass } from '@/lib/format'
import { providerLabel } from '@/lib/source-labels'
import { toast } from '@/components/Toast'

import { XTICK_KIND_LABELS, columnsFor } from './columns'

type Tab = 'auction' | 'board' | 'emotion' | 'moneyflow' | 'lhb' | 'news' | 'kline' | 'holder'

const TABS: { id: Tab; label: string }[] = [
  { id: 'auction', label: '竞价' },
  { id: 'board', label: '连板天梯' },
  { id: 'emotion', label: '市场情绪' },
  { id: 'moneyflow', label: '资金流' },
  { id: 'lhb', label: '龙虎榜' },
  { id: 'news', label: '资讯' },
  { id: 'kline', label: 'K线 / 财务' },
  { id: 'holder', label: '股东结构' },
]

const inputCls = 'h-8 rounded-md border border-border bg-elevated px-2 text-xs text-foreground outline-none focus:border-accent'

type Params = Record<string, string | number | boolean | undefined>

function cnDate(offsetDays = 0): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(Date.now() + offsetDays * 86_400_000))
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

function itemsOf(data: XTickData | null): XTickRow[] {
  if (!data) return []
  if ('items' in data && Array.isArray(data.items)) return data.items as XTickRow[]
  return []
}

function Loading() {
  return <div className="flex items-center justify-center gap-2 py-12 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" />加载中…</div>
}

type QueryLike = UseQueryResult<XTickPayload<XTickData>, Error>

function XTickResult({ query, children, emptyHint }: {
  query: QueryLike
  children: (payload: XTickPayload<XTickData>) => ReactNode
  emptyHint: string
}) {
  if (query.isLoading) return <Loading />
  if (query.isError) {
    return <ErrorState title="XTick 加载失败" hint="请检查网络后重试；若提示未配置数据源，请到设置页填写 API Key" onRetry={() => void query.refetch()} />
  }
  const payload = query.data
  if (!payload) return <EmptyState title="暂无数据" hint={emptyHint} />
  if (['source_unavailable', 'upstream_error', 'invalid_request'].includes(payload.state)) {
    return <ErrorState title="XTick 数据暂不可用" hint={payload.message ?? undefined} retrying={query.isFetching} onRetry={() => void query.refetch()} />
  }
  if (payload.state === 'no_data') return <EmptyState title="暂无数据" hint={payload.message ?? emptyHint} />
  return <>{children(payload)}</>
}

/** 涨跌语义色只给有方向的列 (涨跌幅/同比/净额), 其余列保持中性 */
function cellColor(spec: FieldSpec, value: unknown): string {
  if (!spec.colored) return ''
  const num = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(num) ? priceColorClass(num) : ''
}

function RowTable({ rows, specs, limit = 60 }: {
  rows: XTickRow[]
  specs: FieldSpec[]
  limit?: number
}) {
  const shown = rows.slice(0, limit)
  const columns = resolveColumns(shown, specs)
  if (!shown.length || !columns.length) return <EmptyState title="暂无数据" hint="上游返回空列表，可调整查询条件后重试" />
  return (
    <>
      <div className="mb-2 text-[11px] text-muted">
        共 {rows.length} 条{rows.length > limit ? `，仅展示前 ${limit} 条` : ''}
      </div>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[720px] text-left text-[11px]">
          <thead className="bg-elevated/60 text-muted">
            <tr>
              {columns.map(column => (
                <th
                  key={column.key}
                  title={column.hint}
                  className={cn('whitespace-nowrap px-2 py-1.5 font-medium',
                    alignOf(column) === 'right' && 'text-right')}
                >
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, i) => (
              <tr key={i} className="border-t border-border/60 text-secondary">
                {columns.map(column => (
                  <td
                    key={column.key}
                    className={cn('whitespace-nowrap px-2 py-1.5 tabular-nums',
                      alignOf(column) === 'right' && 'text-right',
                      cellColor(column, row[column.key]))}
                  >
                    {formatFieldValue(row[column.key], column)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function DataPanel({ kind, params, hint, interval, limit }: {
  kind: string; params: Params; hint: string; interval?: number | false; limit?: number
}) {
  const query = useQuery({
    queryKey: QK.xtickData(kind, params),
    queryFn: () => api.xtickData(kind, params),
    refetchInterval: interval,
    placeholderData: prev => prev,
  })
  return (
    <XTickResult query={query} emptyHint={hint}>
      {payload => (
        <div>
          <div className="mb-2 flex flex-wrap items-center gap-3 text-[11px] text-muted">
            <span className="text-secondary">{XTICK_KIND_LABELS[kind] ?? kind}</span>
            <span>{payload.cached ? '命中缓存' : '实时拉取'}</span>
            <span>缓存 {payload.cache_ttl_seconds} 秒</span>
            {payload.message && <span className="text-warning">{payload.message}</span>}
          </div>
          <RowTable rows={itemsOf(payload.data)} specs={columnsFor(kind)} limit={limit} />
        </div>
      )}
    </XTickResult>
  )
}

function AuctionPanel() {
  const [date, setDate] = useState(() => cnDate())
  const [code, setCode] = useState('all')
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={date} onChange={e => setDate(e.target.value)} className={inputCls} aria-label="交易日期" />
        <input value={code} onChange={e => setCode(e.target.value)} placeholder="股票代码，可填 all" className={cn(inputCls, 'w-56')} aria-label="股票代码" />
      </div>
      <p className="mt-2 text-[11px] text-muted">集合竞价 9:15–9:25 期间返回最新撮合；代码填 all 时返回全市场，页面仅展示前 60 条。</p>
      <div className="mt-3">
        <DataPanel kind="bid_time" params={{ type: 1, code: code.trim() || 'all' }}
          hint="非竞价时段可能返回上一交易日数据" interval={30_000} />
      </div>
    </section>
  )
}

function BoardPanel() {
  const [date, setDate] = useState(() => cnDate())
  const [flag, setFlag] = useState('1')
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={date} onChange={e => setDate(e.target.value)} className={inputCls} aria-label="交易日期" />
        <select value={flag} onChange={e => setFlag(e.target.value)} className={inputCls} aria-label="上榜类型">
          <option value="1">涨停板</option><option value="2">跌停板</option><option value="3">炸板</option>
        </select>
      </div>
      <div className="mt-3">
        <DataPanel kind="board" params={{ type: 1, flag: Number(flag), tradeDate: date }}
          hint="当日无涨停/跌停/炸板数据时为空" interval={60_000} />
      </div>
    </section>
  )
}

function EmotionPanel() {
  const [date, setDate] = useState(() => cnDate())
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={date} onChange={e => setDate(e.target.value)} className={inputCls} aria-label="交易日期" />
      </div>
      <div className="mt-3">
        <DataPanel kind="emotion" params={{ type: 1, tradeDate: date }} hint="非交易日可能无情绪数据" interval={60_000} />
      </div>
    </section>
  )
}

function MoneyflowPanel() {
  const [code, setCode] = useState('000001')
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input value={code} onChange={e => setCode(e.target.value)} placeholder="股票代码，最多 50 个，逗号分隔" className={cn(inputCls, 'min-w-0 flex-1')} aria-label="股票代码" />
      </div>
      <p className="mt-2 text-[11px] text-muted">资金流接口不支持 all；金额单位为元，成交量单位为手。</p>
      <div className="mt-3">
        <DataPanel kind="moneyflow" params={{ type: 1, code: code.trim() }} hint="输入有效股票代码后再查询" interval={30_000} />
      </div>
    </section>
  )
}

function LhbPanel() {
  const [date, setDate] = useState(() => cnDate())
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={date} onChange={e => setDate(e.target.value)} className={inputCls} aria-label="交易日期" />
      </div>
      <p className="mt-2 text-[11px] text-muted">龙虎榜为盘后数据；当日尚未发布时上游可能返回空列表，并非无上榜。</p>
      <div className="mt-3">
        <DataPanel kind="lhb" params={{ tradeDate: date }} hint="换一个已收盘交易日再试" />
      </div>
    </section>
  )
}

function NewsPanel() {
  const [minutes, setMinutes] = useState('30')
  const [date, setDate] = useState(() => cnDate())
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input value={minutes} onChange={e => setMinutes(e.target.value)} className={cn(inputCls, 'w-24')} aria-label="分钟范围" />
        <span className="text-[11px] text-muted">分钟内最新资讯</span>
        <input type="date" value={date} onChange={e => setDate(e.target.value)} className={inputCls} aria-label="交易日期" />
      </div>
      <div className="mt-3">
        <DataPanel kind="news" params={{ minutes: Number(minutes) || 30, tradeDate: date }}
          hint="调大分钟范围或换一个交易日" interval={60_000} />
      </div>
    </section>
  )
}

function KlinePanel() {
  const [code, setCode] = useState('000001')
  const [start, setStart] = useState(() => cnDate(-30))
  const [end, setEnd] = useState(() => cnDate())
  return (
    <section className="space-y-5">
      <div className="rounded-lg border border-border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <input value={code} onChange={e => setCode(e.target.value)} placeholder="股票代码" className={cn(inputCls, 'w-40')} aria-label="股票代码" />
          <input type="date" value={start} onChange={e => setStart(e.target.value)} className={inputCls} aria-label="开始日期" />
          <span className="text-[11px] text-muted">至</span>
          <input type="date" value={end} onChange={e => setEnd(e.target.value)} className={inputCls} aria-label="结束日期" />
        </div>
        <p className="mt-2 text-[11px] text-muted">日K为单标的接口，不作为全市场路由源；价格不复权，成交量单位为手。</p>
        <div className="mt-3">
          <DataPanel kind="kline" params={{ type: 1, code: code.trim(), fq: 1, period: '1d', startDate: start, endDate: end }}
            hint="调整股票代码或日期范围" />
        </div>
      </div>
      <div className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">财务指标</h2>
        <DataPanel kind="gaap" params={{ code: code.trim(), startDate: start, endDate: end }} hint="该标的暂无财务指标" />
      </div>
    </section>
  )
}

function HolderPanel() {
  const [code, setCode] = useState('000001')
  const start = '2015-01-01'
  const end = cnDate()
  const sync = useMutation({
    mutationFn: () => api.holderMetricsSync(),
    onSuccess: res => {
      if (res.status === 'running') {
        toast('股东数据同步已在进行中', 'success')
        return
      }
      toast('已开始同步: 逐股拉取十大股东/流通股东/股东户数, 全市场约 30~60 分钟', 'success')
    },
    onError: () => toast('同步启动失败: 请先在「设置 → 数据源」配置 XTick', 'error'),
  })
  return (
    <section className="space-y-5">
      <div className="rounded-lg border border-border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <input value={code} onChange={e => setCode(e.target.value)} placeholder="股票代码" className={cn(inputCls, 'w-40')} aria-label="股票代码" />
          <span className="text-[11px] text-muted">股东数据按报告期披露，单标的查询</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => sync.mutate()} disabled={sync.isPending}
            className="rounded-md border border-border bg-elevated px-2.5 py-1 text-[11px] text-foreground hover:border-accent disabled:opacity-60">
            {sync.isPending ? '启动中…' : '同步为股东结构因子'}
          </button>
          <span className="text-[11px] text-muted">
            落盘 data/financials/holder_metrics，供选股/回测的「股东结构」因子使用
          </span>
        </div>
      </div>
      <div className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">十大股东</h2>
        <DataPanel kind="top_holder" params={{ code: code.trim(), startDate: start, endDate: end }}
          hint="该标的暂无十大股东数据" limit={30} />
      </div>
      <div className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">十大流通股东</h2>
        <DataPanel kind="top_flow_holder" params={{ code: code.trim(), startDate: start, endDate: end }}
          hint="该标的暂无十大流通股东数据" limit={30} />
      </div>
      <div className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">股东户数</h2>
        <DataPanel kind="holder_num" params={{ code: code.trim(), startDate: start, endDate: end }}
          hint="该标的暂无股东户数数据" limit={20} />
      </div>
    </section>
  )
}

export function XTickPage() {
  const [tab, setTab] = useState<Tab>('board')
  const status = useQuery<XTickStatus>({
    queryKey: QK.xtickStatus,
    queryFn: () => api.xtickStatus(),
    staleTime: 60_000,
  })
  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="XTick 数据"
        subtitle="竞价 · 连板 · 情绪 · 资金流 · 龙虎榜 · 资讯 · K线财务 · 股东结构（实时 30 秒，历史 7 天）"
        titleExtra={(
          <>
            <SourceTag source={providerLabel('xtick')} detail="上游 api.xtick.top 专用能力接口；实时类缓存 30 秒，历史类 7 天" />
            {status.data?.available ? (
              <span className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-secondary">已接入</span>
            ) : (
              <span className="rounded-full bg-danger/10 px-2 py-0.5 text-[10px] text-danger">未配置数据源</span>
            )}
          </>
        )}
      />
      <div className="flex flex-wrap gap-1 border-b border-border px-4 py-2">
        {TABS.map(item => (
          <button key={item.id} type="button" onClick={() => setTab(item.id)}
            className={cn('rounded-md px-3 py-1.5 text-xs transition-colors',
              tab === item.id ? 'bg-accent/10 text-accent' : 'text-muted hover:bg-elevated hover:text-foreground')}>
            {item.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-6xl space-y-3">
          <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>XTick 上游仅提供 HTTP 接口，API Token 通过 URL 参数传输。请仅在可信网络使用，避免代理或日志留存凭据。</span>
          </div>
          {tab === 'auction' && <AuctionPanel />}
          {tab === 'board' && <BoardPanel />}
          {tab === 'emotion' && <EmotionPanel />}
          {tab === 'moneyflow' && <MoneyflowPanel />}
          {tab === 'lhb' && <LhbPanel />}
          {tab === 'news' && <NewsPanel />}
          {tab === 'kline' && <KlinePanel />}
          {tab === 'holder' && <HolderPanel />}
        </div>
      </div>
    </div>
  )
}
