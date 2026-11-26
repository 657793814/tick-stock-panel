import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Clock, Database, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { marketLabel, useMarket } from '@/lib/market'
import { cn } from '@/lib/cn'
import { formatScheduleDatePart, formatScheduleTimePart, isToday } from '@/lib/format'

// 港美股数据状态条 (多市场扩展)。
// 背景: 港美股与 A 股共用 kline_daily 物理存储, 但港美股数据需要单独同步
// (维表 instruments_index_{market} → 日K → enriched)。从未同步时各页面只是
// 空白, 用户看不出「是没数据还是策略没结果」。这里把「有没有 / 到哪天 / 怎么补」
// 直接摆在市场切换器下方。

const SYNC_HINT: Record<string, string> = {
  hk: '首次同步约 1~3 分钟（约 2900 只标的）',
  us: '首次同步约 5~10 分钟（约 1.2 万只标的）',
}

export function MarketDataBanner() {
  const { market } = useMarket()
  const isCn = market === 'cn'
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)

  const status = useQuery({
    queryKey: QK.marketDataStatus(market),
    queryFn: () => api.marketDataStatus(market),
    // A 股也要显示同步时点(上次/下次), 故不再排除 cn
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  })

  const start = useMutation({
    mutationFn: () => api.pipelineRunMarket(market),
    onSuccess: data => {
      setJobId(data.job_id)
      setStartedAt(Date.now())
    },
  })

  // 差额补齐指标: 日K 已同步但 enriched 缺失 (分批同步时某批指标计算失败/超时)
  const backfill = useMutation({
    mutationFn: () => api.pipelineBackfillEnriched(market),
    onSuccess: data => {
      setJobId(data.job_id)
      setStartedAt(Date.now())
    },
  })

  const job = useQuery({
    queryKey: QK.pipelineJob(jobId ?? ''),
    queryFn: () => api.pipelineJob(jobId as string),
    enabled: !!jobId,
    refetchInterval: query => {
      const s = query.state.data?.status
      return s === 'pending' || s === 'running' ? 1500 : false
    },
  })

  const jobDone = job.data?.status === 'succeeded' || job.data?.status === 'failed'
  useEffect(() => {
    if (!jobDone) return
    void qc.invalidateQueries({ queryKey: QK.marketDataStatus(market) })
    void qc.invalidateQueries({ queryKey: QK.marketSnapshot(market) })
    void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
  }, [jobDone, market, qc])

  // 任务结束后保留 summary 展示一小会儿, 但不挡住刷新后的数据概览
  const running = job.data?.status === 'pending' || job.data?.status === 'running'
  const failed = job.data?.status === 'failed'
  const done = job.data?.status === 'succeeded'

  const stageText = useMemo(() => {
    if (!job.data) return '提交同步任务…'
    const last = job.data.log[job.data.log.length - 1]
    const pct = Math.max(0, Math.min(100, Math.round(job.data.progress)))
    return `${pct}% · ${last?.msg ?? job.data.stage}`
  }, [job.data])

  const loading = status.isLoading
  const s = status.data
  const hasData = !!s?.has_data
  const shown = jobId ? job.data : undefined

  // 定时同步时点 (三市场统一展示): 上次完成 / 下次执行。A 股无"同步引导",
  // 只有这一段; 港美股下面继续显示数据概览与同步按钮。
  const syncSchedule = (
    <span className="shrink-0 whitespace-nowrap text-muted/80">
      <Clock className="mr-0.5 inline h-3 w-3" aria-hidden="true" />
      {s?.last_sync_run
        ? `上次 ${isToday(s.last_sync_run) ? '' : formatScheduleDatePart(s.last_sync_run) + ' '}${formatScheduleTimePart(s.last_sync_run)}`
        : '上次 —'}
      {' → 下次 '}
      {s?.next_sync_run
        ? formatScheduleTimePart(s.next_sync_run)
        : '—'}
    </span>
  )

  if (isCn) {
    return (
      <div className="shrink-0 border-b border-border px-3 py-1.5 text-[11px] flex items-center gap-2 text-muted">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="truncate">A股本地数据 · 最新 {s?.latest_date ?? '—'}</span>
        {syncSchedule}
      </div>
    )
  }

  return (
    <div
      className={cn(
        'shrink-0 border-b px-3 py-1.5 text-[11px] flex items-center gap-2',
        hasData ? 'border-border bg-surface/40 text-muted' : 'border-amber/30 bg-amber/5 text-amber/90',
      )}
    >
      {running ? (
        <>
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
          <span className="truncate">正在同步{marketLabel(market)}数据 · {stageText}</span>
        </>
      ) : failed ? (
        <>
          <AlertCircle className="h-3.5 w-3.5 shrink-0 text-danger" />
          <span className="truncate">
            同步失败：{shown?.error ?? '未知错误'}
          </span>
          <button
            type="button"
            onClick={() => start.mutate()}
            disabled={start.isPending}
            className="ml-auto shrink-0 rounded-btn bg-accent px-2 py-0.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-60"
          >
            重试
          </button>
        </>
      ) : hasData ? (
        <>
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-accent" />
          <span className="truncate">
            {marketLabel(market)}本地数据：日K 覆盖 {(s?.symbols ?? 0).toLocaleString('zh-CN')}
            {s && s.instruments > 0 ? ` / ${s.instruments.toLocaleString('zh-CN')}` : ''} 只 · 最新 {s?.latest_date ?? '—'}
            {s && s.enriched_symbols < s.symbols ? ` · 指标 ${s.enriched_symbols.toLocaleString('zh-CN')} 只` : ''}
          </span>
          {done && <span className="shrink-0 text-accent">已更新</span>}
          {syncSchedule}
          {s && s.enriched_symbols < s.symbols && (
            <button
              type="button"
              onClick={() => backfill.mutate()}
              disabled={backfill.isPending || running}
              title="只为「有日K但缺指标」的标的计算指标, 不重算已有指标"
              className="shrink-0 rounded-btn border border-border px-1.5 py-0.5 text-[11px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-60"
            >
              {backfill.isPending ? '提交中…' : '补齐指标'}
            </button>
          )}
          <button
            type="button"
            onClick={() => start.mutate()}
            disabled={start.isPending}
            title="增量同步该市场日K并重算指标"
            className="ml-auto shrink-0 flex items-center gap-1 rounded-btn px-1.5 py-0.5 text-[11px] text-muted hover:bg-elevated hover:text-foreground disabled:opacity-60"
          >
            <RefreshCw className={cn('h-3 w-3', start.isPending && 'animate-spin')} />
            更新
          </button>
        </>
      ) : (
        <>
          <Database className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">
            {loading
              ? `检查${marketLabel(market)}本地数据…`
              : `${marketLabel(market)}本地暂无数据${s && s.instruments > 0 ? `（维表 ${s.instruments.toLocaleString('zh-CN')} 只，日K 未同步）` : ''}`}
          </span>
          <span className="hidden shrink-0 text-amber/60 xl:inline">{SYNC_HINT[market] ?? ''}</span>
          {syncSchedule}
          <button
            type="button"
            onClick={() => start.mutate()}
            disabled={start.isPending || loading}
            className="ml-auto shrink-0 rounded-btn bg-accent px-2 py-0.5 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-60"
          >
            {start.isPending ? '提交中…' : `同步${marketLabel(market)}数据`}
          </button>
        </>
      )}
      {done && startedAt && (
        <span className="sr-only">同步开始于 {new Date(startedAt).toLocaleTimeString('zh-CN')}</span>
      )}
    </div>
  )
}
