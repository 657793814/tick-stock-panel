/**
 * 东方财富日频事件页 —— 龙虎榜 / 融资融券。
 *
 * 归属: 扩展域消费页 (官方), 经 custom/eastmoney/extension.tsx 注册路由与导航。
 * 数据落盘 data/financials/{lhb_metrics,margin_metrics}, 由 backtest/fundamentals
 * 的日频表机制注册为「龙虎榜」「融资融券」因子组 (T 日数据 T+1 生效)。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, RefreshCw } from 'lucide-react'

import { api, type EastmoneyStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { SourceTag } from '@/components/SourceTag'
import { cn } from '@/lib/cn'
import { alignOf, formatFieldValue, resolveColumns } from '@/lib/field-labels'
import { priceColorClass } from '@/lib/format'
import { providerLabel } from '@/lib/source-labels'
import { toast } from '@/components/Toast'

import { FACTOR_HINTS, eastmoneyColumns } from './columns'

const inputCls =
  'h-8 rounded-md border border-border bg-elevated px-2 text-xs text-foreground outline-none focus:border-accent'

function Loading() {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted">
      <Loader2 className="h-4 w-4 animate-spin" />加载中…
    </div>
  )
}

function TableCard({ table, meta, symbol }: {
  table: string
  meta?: EastmoneyStatus['tables'][string]
  symbol: string
}) {
  const query = useQuery({
    queryKey: QK.eastmoneyData(table, { symbol, limit: 200 }),
    queryFn: () => api.eastmoneyData(table, { symbol: symbol || undefined, limit: 200 }),
    placeholderData: prev => prev,
  })

  const rows = query.data?.rows.slice(0, 60) ?? []
  const columns = resolveColumns(rows, eastmoneyColumns(table))

  return (
    <section className="rounded-lg border border-border p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-xs font-medium text-foreground">{meta?.label ?? table}</h2>
        <span className="text-[11px] text-muted">
          {meta?.rows ? `${meta.rows} 行 · ${meta.symbols} 标的 · ${meta.start} ~ ${meta.end}` : '尚未同步'}
        </span>
      </div>
      <ul className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        {(FACTOR_HINTS[table] ?? []).map(item => (
          <li key={item.factor} title={`因子 ${item.factor}`}>· {item.label}</li>
        ))}
      </ul>
      {query.isLoading ? (
        <Loading />
      ) : query.isError ? (
        <ErrorState title="加载失败" hint="请稍后重试" onRetry={() => void query.refetch()} />
      ) : !rows.length ? (
        <EmptyState title="暂无数据" hint="点击上方「同步」拉取最近的交易日数据" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full min-w-[860px] text-left text-[11px]">
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
              {rows.map((row, index) => (
                <tr key={index} className="border-t border-border/60 text-secondary">
                  {columns.map(column => {
                    const value = row[column.key]
                    const num = typeof value === 'number' ? value : Number(value)
                    return (
                      <td
                        key={column.key}
                        className={cn('whitespace-nowrap px-2 py-1.5 tabular-nums',
                          alignOf(column) === 'right' && 'text-right',
                          column.colored && Number.isFinite(num) ? priceColorClass(num) : '')}
                      >
                        {formatFieldValue(value, column)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function EastmoneyPage() {
  const qc = useQueryClient()
  const [days, setDays] = useState(20)
  const [symbol, setSymbol] = useState('')

  const status = useQuery({
    queryKey: QK.eastmoneyStatus,
    queryFn: api.eastmoneyStatus,
    refetchInterval: 10_000,
  })

  const sync = useMutation({
    mutationFn: (table: string) => api.eastmoneySync(table, days),
    onSuccess: res => {
      toast(
        res.status === 'running' ? '已有同步任务在执行' : `已开始同步，窗口 ${res.days ?? days} 个交易日`,
        res.status === 'running' ? 'error' : 'success',
      )
      void qc.invalidateQueries({ queryKey: QK.eastmoneyStatus })
    },
    onError: () => toast('同步启动失败', 'error'),
  })

  const tables = status.data?.tables ?? {}

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="东方财富"
        subtitle="龙虎榜 · 融资融券 → 日频事件因子（公开接口，免 token）"
        titleExtra={(
          <SourceTag
            source={providerLabel('eastmoney')}
            detail="东财 datacenter-web 公开接口；落盘 data/financials/{lhb_metrics,margin_metrics}，T 日数据 T+1 生效"
          />
        )}
      />
      <div className="flex-1 space-y-4 overflow-auto p-4">
        <section className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
          <label className="text-[11px] text-muted" htmlFor="em-days">同步窗口</label>
          <input
            id="em-days" type="number" min={1} max={400} value={days}
            onChange={event => setDays(Math.max(1, Math.min(400, Number(event.target.value) || 1)))}
            className={cn(inputCls, 'w-20')}
          />
          <span className="text-[11px] text-muted">个交易日</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => sync.mutate('lhb_metrics')}
              disabled={sync.isPending || status.data?.syncing}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-elevated px-2.5 py-1 text-[11px] text-foreground hover:border-accent disabled:opacity-60"
            >
              <RefreshCw className={cn('h-3 w-3', sync.isPending && 'animate-spin')} />同步龙虎榜
            </button>
            <button
              type="button"
              onClick={() => sync.mutate('margin_metrics')}
              disabled={sync.isPending || status.data?.syncing}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-elevated px-2.5 py-1 text-[11px] text-foreground hover:border-accent disabled:opacity-60"
            >
              <RefreshCw className={cn('h-3 w-3', sync.isPending && 'animate-spin')} />同步融资融券
            </button>
          </div>
          <p className="w-full text-[11px] text-muted">
            东财对高频请求会临时封 IP：龙虎榜 1 请求/日，融资融券约 9 请求/日，逐日节流。
            首次回补更长历史请调大窗口（上限 400 个交易日）。
          </p>
        </section>

        <section className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
          <label className="text-[11px] text-muted" htmlFor="em-symbol">标的过滤</label>
          <input
            id="em-symbol" value={symbol} onChange={event => setSymbol(event.target.value.trim())}
            placeholder="如 600519.SH，留空看全市场" className={cn(inputCls, 'w-56')}
          />
          <span className="text-[11px] text-muted">
            因子口径：交易日 T 的数据在 T+1 生效；龙虎榜未上榜按 0，两融未覆盖保持空值。
          </span>
        </section>

        {status.isError ? (
          <ErrorState title="状态加载失败" hint="请稍后重试" onRetry={() => void status.refetch()} />
        ) : (
          <>
            <TableCard table="lhb_metrics" meta={tables.lhb_metrics} symbol={symbol} />
            <TableCard table="margin_metrics" meta={tables.margin_metrics} symbol={symbol} />
          </>
        )}
      </div>
    </div>
  )
}
