/**
 * 模拟盘 (虚拟账户) — 多账户对比主视图 + 行展开账户操作。
 *
 * 对比 (默认): 全账户收益降序大列表 (每行一个账户, 同本金同费率口径),
 * 盘中快照就绪时总资产/收益显示实时估算 (估 徽标), 归一化净值叠加;
 * 行内直达冻结/删除 (强确认弹窗, 有持仓时后端先拒绝再显式强删);
 * 批量创建一次构造一批同规格账户各绑一条跟单规则 (策略直接跟实时行情模拟)。
 *
 * 行展开 = 完整账户操作: 总览/净值/持仓/订单 (全状态全量)/成交台账 (可导出
 * CSV)/回合明细 (FIFO)/下单 (卖出带 T+1 可卖提示)/跟单规则/费用/冻结/删除。
 *
 * 口径提示: 定版字段按交易日收盘结算; 盘中实时估算与 /overview 同口径。
 * 费用/滑点参数与回测引擎同名同默认值。
 * 多账户: 所有查询按账户隔离 (queryKey 前缀 'paper'), 切换即换一套数据。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Activity, Banknote, ChevronDown, ChevronUp, CircleDollarSign, Cpu, GitCompare, History, ListChecks, PieChart, Plus, RefreshCw, Settings, ShieldAlert, SlidersHorizontal, TrendingUp, Wallet, X, Zap } from 'lucide-react'
import { createPortal } from 'react-dom'
import { api, type PaperCompareRow, type PaperFill, type PaperHolding, type PaperLiveItem, type PaperLiveTotals, type PaperOrder, type PaperReplayResponse, type PaperRound } from '@/lib/api'
import type { ChartMarker } from '@/lib/chart-types'
import { StockDailyKChart, getDefaultRange } from '@/components/StockDailyKChart'
import { QK } from '@/lib/queryKeys'
import { selectableStrategies, strategyOptionLabel } from '@/lib/strategyLibrary'
import { useQuoteStatus } from '@/lib/useSharedQueries'
import { useSymbolTicks } from '@/lib/useSymbolTicks'
import { useMarket, marketLabel, type Market } from '@/lib/market'
import { cn } from '@/lib/cn'
import { fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { StrategyExecutionPanel } from './StrategyExecutionPanel'
import { executionApi, executionKey, executionListKey, type ExecutionBinding } from './executionClient'
import { AnimatedNumber } from '@/components/motion/AnimatedNumber'
import { PulseLine } from '@/components/motion/PulseLine'

const ORDER_TYPE_LABEL: Record<string, string> = {
  market: '即时',
  next_open: '次日开盘',
  close: '当日收盘',
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待成交',
  filled: '已成交',
  cancelled: '已撤单',
  expired: '已过期',
}

function fmtMoney(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 账户 id: 字母数字开头短横线/下划线, 与后端校验一致 */
function genAccountId() {
  return `acc_${Date.now().toString(36)}${Math.floor(Math.random() * 36).toString(36)}`
}

/** 归一化到首日=100 (基准对比); 空洞(缺行情日)保留 null 不连线 */
function normalizeBase(values: Array<number | null | undefined>): Array<number | null> {
  const first = values.find((v): v is number => v != null && v > 0)
  if (!first) return values.map(() => null)
  return values.map(v => (v != null && v > 0 ? Number(((v / first) * 100).toFixed(2)) : null))
}

/** 净值曲线基准名称（与 app.markets.benchmark_symbol 对齐） */
const BENCHMARK_LABEL: Record<Market, string> = { cn: '沪深300', hk: '恒生指数', us: '标普500' }

/** 净值曲线 (echarts line, 跟随主题色): 账户归一化 vs 市场基准归一化 (首日=100) */
function NavChart({ nav, market = 'cn' }: {
  nav: Array<{ date: string; nav: number; benchmark_close?: number }>
  market?: Market
}) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!elRef.current) return
    chartRef.current = echarts.init(elRef.current, undefined, { renderer: 'canvas' })
    const onResize = () => chartRef.current?.resize()
    window.addEventListener('resize', onResize)
    // 跟随亮暗主题切换重设坐标轴配色
    const observer = new MutationObserver(() => {
      const darkNow = document.documentElement.classList.contains('dark')
      const axisColor = darkNow ? '#8E8E96' : '#52525B'
      const splitColor = darkNow ? 'rgba(139, 92, 246, 0.16)' : '#E4E4E7'
      chartRef.current?.setOption({
        xAxis: { axisLine: { lineStyle: { color: splitColor } }, axisLabel: { color: axisColor } },
        yAxis: { axisLabel: { color: axisColor }, splitLine: { lineStyle: { color: splitColor } } },
      })
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => {
      window.removeEventListener('resize', onResize)
      observer.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const dark = document.documentElement.classList.contains('dark')
    const axisColor = dark ? '#8E8E96' : '#52525B'
    const splitColor = dark ? 'rgba(139, 92, 246, 0.16)' : '#E4E4E7'
    chart.setOption({
      grid: { left: 56, right: 16, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: number) => fmtMoney(v),
      },
      xAxis: {
        type: 'category',
        data: nav.map(n => n.date),
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, fontSize: 10 },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: axisColor, fontSize: 10, formatter: (v: number) => fmtMoney(v, 0) },
        splitLine: { lineStyle: { color: splitColor } },
      },
      series: [
        {
          name: '虚拟账户',
          type: 'line',
          data: normalizeBase(nav.map(n => n.nav)),
          showSymbol: false,
          lineStyle: { color: '#A78BFA', width: 2.4, shadowBlur: 14, shadowColor: 'rgba(139, 92, 246, 0.6)' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(139, 92, 246, 0.26)' },
              { offset: 1, color: 'rgba(139, 92, 246, 0.01)' },
            ]),
          },
        },
        ...(nav.some(n => n.benchmark_close) ? [{
          name: BENCHMARK_LABEL[market],
          type: 'line' as const,
          data: normalizeBase(nav.map(n => n.benchmark_close)),
          showSymbol: false,
          lineStyle: { color: dark ? '#8E8E96' : '#52525B', width: 1.5, type: 'dashed' as const },
        }] : []),
      ],
      legend: {
        top: 0,
        right: 0,
        textStyle: { color: axisColor, fontSize: 10 },
        itemWidth: 14,
      },
    })
  }, [nav, market])

  return <div ref={elRef} className="h-48 w-full" />
}

function StatCard({ label, value, numeric, numFormat, sub, valueClass, icon: Icon, iconCls }: {
  label: string; value?: string; numeric?: number; numFormat?: (v: number) => string; sub?: string; valueClass?: string
  icon?: React.ComponentType<{ className?: string }>; iconCls?: string
}) {
  return (
    <div className="tf-stat rounded-card border border-border bg-surface px-4 py-3 transition-colors duration-150 hover:border-accent/25 hover:bg-elevated/30">
      <div className="flex items-center gap-1.5 text-[10px] tracking-[0.1em] text-muted">
        {Icon && <Icon className={cn('h-3.5 w-3.5', iconCls)} />}
        {label}
      </div>
      <div className={cn('tf-stat-value mt-1 font-mono text-xl font-semibold tabular', valueClass)}>
        {numeric != null && numFormat ? (
          <AnimatedNumber value={numeric} format={numFormat} className="tabular" />
        ) : value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-muted">{sub}</div>}
    </div>
  )
}


/** 全账户盈亏速览墙: 按累计收益排序的一屏对比面板 (任务: 快速查阅谁在盈谁在亏)。
 *  领涨红/领跌绿 (A 股配色), 卡片点击直接打开该账户终端。 */
const PNL_MKT_LABEL: Record<string, string> = { cn: 'A股', hk: '港股', us: '美股' }

function PnlBoard({ rows, onOpen }: { rows: PaperCompareRow[]; onOpen: (r: PaperCompareRow) => void }) {
  const board = useMemo(
    () => [...rows].sort((a, b) => (b.pnl_pct ?? -9999) - (a.pnl_pct ?? -9999)),
    [rows],
  )
  if (board.length === 0) return null
  const pcts = board.map(r => Math.abs(r.pnl_pct ?? 0))
  const span = Math.max(...pcts, 0.01)
  const gainers = board.filter(r => (r.pnl_pct ?? 0) > 0).length
  const losers = board.filter(r => (r.pnl_pct ?? 0) < 0).length
  return (
    <div className="tf-pnl-board">
      <div className="flex items-center gap-2">
        <span className="tf-pnl-tag">P&L BOARD</span>
        <span className="text-[10px] text-muted">全账户盈亏速览 · 点卡片开终端</span>
        <span className="ml-auto flex items-center gap-3 font-mono text-[10px] tabular">
          <span className="text-bull">盈 {gainers}</span>
          <span className="text-bear">亏 {losers}</span>
          <span className="text-muted">平 {board.length - gainers - losers}</span>
        </span>
      </div>
      <div className="tf-pnl-grid">
        {board.map((r, i) => {
          const pct = r.estimating ? (r.live_pnl_pct ?? r.pnl_pct) : r.pnl_pct
          const pnl = r.total_pnl ?? 0
          const up = (pct ?? 0) >= 0
          const w = pct == null ? 0 : Math.min(100, Math.max(5, (Math.abs(pct) / span) * 100))
          return (
            <button
              key={r.account}
              type="button"
              onClick={() => onOpen(r)}
              className={cn('tf-pnl-cell', up ? 'is-up' : 'is-down', r.status === 'frozen' && 'is-frozen')}
              title={`${r.name} · ${r.account} · 总资产 ${fmtMoney(r.total ?? 0)}`}
            >
              <span className="tf-pnl-rank">{i + 1}</span>
              <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                <span className="flex w-full items-center gap-1">
                  <span className="max-w-28 truncate text-[11px] font-medium text-secondary">{r.name}</span>
                  <span className="tf-pnl-mkt">{PNL_MKT_LABEL[r.market ?? 'cn']}</span>
                  {r.estimating && <span className="tf-pnl-live">LIVE</span>}
                  {r.status === 'frozen' && <span className="tf-pnl-mkt is-frozen">冻结</span>}
                </span>
                <AnimatedNumber
                  value={pnl}
                  format={v => `${v >= 0 ? '+' : ''}${fmtMoney(v)}`}
                  resetKey={r.estimating ? 'live' : (r.last_nav_date ?? undefined)}
                  className={cn('font-mono text-[13px] font-semibold tabular', priceColorClass(pnl / (r.initial_cash || 1)))}
                />
              </span>
              <span className="flex shrink-0 flex-col items-end gap-1">
                <span className={cn('font-mono text-[11px] tabular', priceColorClass((pct ?? 0) / 100))}>
                  {pct == null ? '—' : fmtPct(pct / 100)}
                </span>
                <span className="tf-pnl-bar" aria-hidden="true">
                  <i style={{ width: `${w}%` }} className={up ? 'bg-bull' : 'bg-bear'} />
                </span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** 新建账户默认值 (localStorage), 字段级优先级: 基础配置 (用户显式设置) > 上次创建记忆 > 内置默认。 */
const NEW_ACCOUNT_DEFAULTS_KEY = 'paper.newAccountDefaults'
const PAPER_BASE_DEFAULTS_KEY = 'paper.baseDefaults.v1'
type NewAccountDefaults = {
  cash?: string; commissionWan?: string; stampQian?: string; slippageBps?: string
}

function loadNewAccountDefaults(): Required<NewAccountDefaults> {
  let base: NewAccountDefaults = {}
  let last: NewAccountDefaults = {}
  try { base = JSON.parse(localStorage.getItem(PAPER_BASE_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  try { last = JSON.parse(localStorage.getItem(NEW_ACCOUNT_DEFAULTS_KEY) || '{}') } catch { /* 忽略坏数据 */ }
  const src = { ...last, ...base }
  return {
    cash: src.cash ?? '1000000',
    commissionWan: src.commissionWan ?? '2.5',   // 万2.5
    stampQian: src.stampQian ?? '1',             // 千1 (A股卖出单边)
    slippageBps: src.slippageBps ?? '5',         // 5bps
  }
}

/** 切换市场时同步默认印花税档位（美股无印花税，港股双边千1）。 */
function defaultStampQian(market: Market, fallback: string): string {
  if (market === 'us') return '0'
  if (market === 'hk') return '1'
  return fallback
}

/** 初始化向导: 选中账户不存在或正在新建时展示。
 * onCancel 由外层按「是否已有其他账户」传入 —— 一个账户都没有时必须先建一个, 不给取消。
 * 费用三参数可设置, 且记住为下次新建的默认值。 */
function SetupCard({ accId, onDone, onCancel }: { accId: string; onDone: (createdId: string) => void; onCancel?: () => void }) {
  const defaults = loadNewAccountDefaults()
  const { market: globalMarket } = useMarket()
  const [market, setMarket] = useState<Market>(globalMarket)
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaultStampQian(globalMarket, defaults.stampQian))
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [name, setName] = useState('')
  // 事件跟单（兼容，可选）: 创建后立即绑一条自动跟单规则, 策略/监控规则触发信号时按下方参数自动下单
  const [autoKind, setAutoKind] = useState<'strategy' | 'rule'>('strategy')
  const [autoId, setAutoId] = useState('')
  const [autoName, setAutoName] = useState('')
  const [autoSearch, setAutoSearch] = useState('')   // 未选中时的搜索词 (选中后显示中文名)
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('pct_equity')
  const [sizeValue, setSizeValue] = useState('10')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')
  // 账户已建但规则绑定失败: 不静默吞掉 (账户已存在无法重提交), 行内警告 + 手动完成
  const [ruleWarn, setRuleWarn] = useState<string | null>(null)

  const strategiesQ = useQuery({
    // 与 Screener/DefaultStrategyParamsDialog 同键同请求 (api.screenerStrategies(
    // undefined, 'all')), 共享缓存而不是各拼哨兵值各拉一份
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, staleTime: 30_000 })

  const m = useMutation({
    mutationFn: async () => {
      // 账户名未填且已选策略/规则 → 自动用其名称 (不落到 "账户 acc_xxx")
      const autoLabel = autoId.trim() ? (autoName || autoId.trim()) : ''
      const r = await api.paperCreateAccount({
        initial_cash: Number(cash),
        account_id: accId,
        name: name.trim() || autoLabel || undefined,
        market,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
      })
      if (autoId.trim()) {
        try {
          await api.paperAutoRuleCreate({
            name: `跟${autoKind === 'strategy' ? '策略' : '规则'}·${autoName || autoId.trim()}`,
            match_kind: autoKind,
            match_id: autoId.trim(),
            side,
            size_mode: sizeMode,
            size_value: Number(sizeValue),
            order_type: orderType,
            cooldown_days: Number(cooldown),
            max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
            enabled: true,
          }, r.account.id)
        } catch (err) {
          return { r, ruleError: String((err as Error).message || err) }
        }
      }
      return { r, ruleError: null as string | null }
    },
    onSuccess: ({ r, ruleError }) => {
      // 记住本次取值, 作为下次新建账户的默认
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({
          cash, commissionWan, stampQian, slippageBps,
        }))
      } catch { /* 存储不可用时静默 */ }
      if (ruleError) { setRuleWarn(ruleError); return }
      onDone(r.account.id)
    },
  })

  const autoQuery = (autoId ? (autoName || autoId) : autoSearch).trim().toLowerCase()
  const autoOptions: SuggestOption[] = autoKind === 'strategy'
    ? selectableStrategies(strategiesQ.data?.presets ?? [], [autoId])
        .filter(s => !autoQuery || s.name.toLowerCase().includes(autoQuery) || s.id.toLowerCase().includes(autoQuery) || (s.tags ?? []).some(t => t.toLowerCase().includes(autoQuery)))
        .map(s => ({
          value: s.id,
          label: strategyOptionLabel(s),
          desc: s.description || undefined,
          badges: [
            ...(s.library?.group_name ? [s.library.group_name] : []),
            ...(s.timeframes ?? []).map(tf => tf === '1d' ? '日线' : tf === '1m' ? '分钟' : tf),
            ...(s.tags ?? []).slice(0, 2),
          ].slice(0, 3),
        }))
    : (rulesQ.data?.rules ?? [])
        .filter(r => !autoQuery || r.name.toLowerCase().includes(autoQuery) || r.id.toLowerCase().includes(autoQuery))
        .map(r => ({ value: r.id, label: r.name || r.id }))

  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0
    && (!autoId.trim() || Number(sizeValue) > 0)
  const segBtn = (active: boolean) => cn(
    'whitespace-nowrap rounded-btn border px-2 py-1 text-[11px] transition-colors',
    active ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
  )
  return (
    <div className="p-5">
      <div className="flex items-center gap-2">
        <CircleDollarSign className="h-5 w-5 text-accent" />
        <h2 id="setup-card-title" className="text-[16px] leading-6 font-semibold">创建虚拟账户</h2>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        虚拟资金 + 真实行情价格模拟撮合, 不涉及任何真实资金。费用口径与回测一致,
        创建后仍可在「费用」里调整; 本次取值会记住, 作为下次新建的默认。
      </p>
      <label className="mt-4 block text-xs text-secondary">账户名称 (可选)</label>
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent/50"
        placeholder={autoId.trim() && autoName ? `默认用${autoKind === 'strategy' ? '策略' : '规则'}名: ${autoName}` : (accId === 'default' ? '默认账户' : `账户 ${accId}`)}
      />
      <label className="mt-3 block text-xs text-secondary">交易市场</label>
      <select
        value={market}
        onChange={e => {
          const next = e.target.value as Market
          setMarket(next)
          setStampQian(defaultStampQian(next, defaults.stampQian))
        }}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent/50"
      >
        <option value="cn">A股 · T+1 · 100股/手</option>
        <option value="hk">港股 · T+0 · 100股/手(估算) · 双边印花税</option>
        <option value="us">美股 · T+0 · 1股/手</option>
      </select>
      {market !== 'cn' && (
        <p className="mt-1 text-[10px] text-muted">
          当前按 {marketLabel(market)} 默认口径撮合；{market === 'hk' ? '港股每手实际股数按标的维表，后续接入后再收紧' : '美股按 1 股起算'}。
        </p>
      )}
      <label className="mt-3 block text-xs text-secondary">初始虚拟资金 (元)</label>
      <input
        type="number"
        value={cash}
        onChange={e => setCash(e.target.value)}
        className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-2 font-mono text-sm outline-none focus:border-accent/50"
        placeholder="1000000"
      />
      <div className="mt-3 grid grid-cols-3 gap-2">
        <div>
          <label className="block text-[11px] text-muted">佣金 (万)</label>
          <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">印花税 (千)</label>
          <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="block text-[11px] text-muted">滑点 (bps)</label>
          <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-2 py-2 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
      </div>

      {/* 事件跟单（兼容，可选）: 创建即绑定, 信号触发自动下单 */}
      <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Zap className="h-3.5 w-3.5 text-warning" />
          <span className="text-xs font-medium text-secondary">事件跟单（兼容，可选）</span>
          <span className="text-[10px] text-muted">旧通知事件模式；回测参数快照请在创建后绑定</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <div className="flex gap-1">
            <button onClick={() => { setAutoKind('strategy'); setAutoId(''); setAutoName(''); setAutoSearch(''); setSide('auto') }} className={segBtn(autoKind === 'strategy')}>策略</button>
            <button onClick={() => { setAutoKind('rule'); setAutoId(''); setAutoName(''); setAutoSearch(''); setSide(s => (s === 'auto' ? 'buy' : s)) }} className={segBtn(autoKind === 'rule')}>监控规则</button>
          </div>
          <div className="min-w-44 flex-1">
            {/* 选中后显示中文名 (id 只做匹配键); 重新输入则回到搜索态 */}
            <SuggestInput
              value={autoId ? (autoName || autoId) : autoSearch}
              onChange={v => { setAutoSearch(v); setAutoId(''); setAutoName('') }}
              options={autoOptions}
              onSelect={opt => { setAutoId(opt.value); setAutoName(opt.label === opt.value ? '' : opt.label); setAutoSearch('') }}
              loading={autoKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
              placeholder={autoKind === 'strategy' ? '搜索策略 (中文名 / ID)' : '搜索监控规则'}
              className="w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
            />
          </div>
          {autoId.trim() && (
            <button onClick={() => { setAutoId(''); setAutoName(''); setAutoSearch('') }} className="rounded-btn border border-border px-2 py-1 text-[10px] text-muted hover:text-danger" title="不绑定策略">清除</button>
          )}
        </div>
        {/* 两行栅格: 5 组控件一行放不下会被压成竖排文字并相互叠压。
            跟策略无方向概念 (固定 auto: 入场买/出场全平), 跟监控规则才显示方向。
            输入搜索词即展开 (选中或搜索中都可见参数) */}
        {(autoId || autoSearch).trim() && (
          <div className="mt-2 grid grid-cols-3 gap-2">
            {autoKind === 'strategy' ? (
              // 策略执行语义说明: 双向自动, 无需选择
              <div className="col-span-3 flex items-center gap-1.5 text-[10px] text-muted">
                <Zap className="h-3 w-3 text-accent" />
                入场信号自动买入 · 出场信号自动全平 — 完整执行策略周期
              </div>
            ) : (
              <div>
                <label className="block text-[10px] text-muted">方向</label>
                <div className="mt-1 flex flex-wrap gap-1">
                  <button onClick={() => setSide('buy')} className={segBtn(side === 'buy')}>买入</button>
                  <button onClick={() => setSide('sell')} className={segBtn(side === 'sell')}>卖出</button>
                </div>
              </div>
            )}
            <div>
              <label className="block text-[10px] text-muted">{sizeMode === 'pct_equity' ? '仓位 (权益 %)' : '仓位 (元)'}</label>
              <div className="mt-1 flex gap-1">
                <input type="number" min="0" step={sizeMode === 'pct_equity' ? 1 : 1000} value={sizeValue}
                  onChange={e => setSizeValue(e.target.value)}
                  className="w-16 shrink-0 rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50" />
                <button onClick={() => setSizeMode(m => m === 'pct_equity' ? 'fixed_amount' : 'pct_equity')}
                  className="whitespace-nowrap rounded-btn border border-border px-1.5 py-1 text-[10px] text-muted hover:text-accent" title="切换仓位模式">
                  {sizeMode === 'pct_equity' ? '按比例' : '按金额'}
                </button>
              </div>
            </div>
            <div>
              <label className="block text-[10px] text-muted">最大持仓</label>
              <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
                className="mt-1 w-full rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50"
                title="最多同时持有的标的数 (买入开新仓受限, 加仓/卖出不受限, 0 = 不限) — 与回测 max_positions 同口径" />
            </div>
            <div className="col-span-2">
              <label className="block text-[10px] text-muted">订单类型</label>
              <div className="mt-1 flex flex-wrap gap-1">
                {([['market', '即时'], ['next_open', '次日开盘'], ['close', '当日收盘']] as const).map(([v, t]) => (
                  <button key={v} onClick={() => setOrderType(v)} className={segBtn(orderType === v)}>{t}</button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-[10px] text-muted">冷却 (天)</label>
              <input type="number" min="0" step="1" value={cooldown} onChange={e => setCooldown(e.target.value)}
                className="mt-1 w-full rounded-btn border border-border bg-base px-1.5 py-1 font-mono text-xs outline-none focus:border-accent/50"
                title="同标的触发后 N 个交易日内不重复下单" />
            </div>
          </div>
        )}
      </div>

      <div className="mt-4 flex gap-2">
        <button
          onClick={() => valid && m.mutate()}
          disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
        >
          {m.isPending ? '创建中…' : ruleWarn ? '创建账户' : autoId.trim() ? '创建并绑定策略' : '创建账户'}
        </button>
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={m.isPending}
            className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
            title="放弃创建, 返回原账户"
          >
            取消
          </button>
        )}
      </div>
      {ruleWarn && (
        <div className="mt-2 rounded-btn bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
          账户已创建, 但策略绑定失败: {ruleWarn} — 可在账户面板「自动跟单规则」里手动补建。
          <button onClick={() => onDone(accId)} className="ml-1 underline underline-offset-2">完成</button>
        </div>
      )}
      {m.isError && <div className="mt-2 text-xs text-danger">{String((m.error as Error).message)}</div>}
    </div>
  )
}

// ================================================================
// 通用联想输入 (紧凑版): 受控输入 + 选项下拉 + 键盘导航 + 外点关闭。
// 数据获取由父组件负责 (instrumentSearch 异步搜索 / 策略列表本地过滤),
// 本组件只管交互与渲染; 模式对齐 Watchlist.StockSearchBox 一族搜索框。
// ================================================================
interface SuggestOption {
  /** 选中后写入输入框的值 (完整代码 / 策略 id) */
  value: string
  /** 主文本 (截断) */
  label: string
  /** 可选等宽左槽 (股票代码) */
  left?: string
  /** 可选右槽 (板性徽标 / 来源等) */
  hint?: ReactNode
  /** 可选副行描述 (策略简介), 有则渲染两行式选项 */
  desc?: string
  /** 可选名称右侧徽标组 (tags / 周期) */
  badges?: string[]
}

function SuggestInput({
  value,
  onChange,
  options,
  onSelect,
  placeholder,
  className,
  loading,
}: {
  value: string
  onChange: (v: string) => void
  options: SuggestOption[]
  onSelect: (opt: SuggestOption) => void
  placeholder?: string
  className?: string
  loading?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  // 下拉用 portal + fixed 渲染: 祖先链 (创建向导滚动容器 / Modal 的 max-h overflow)
  // 会硬裁剪 absolute 下拉, 脱离文档流才能完整浮出; 位置实时贴输入框。
  const [dropStyle, setDropStyle] = useState<CSSProperties | null>(null)
  const syncDropPos = useCallback(() => {
    const r = inputRef.current?.getBoundingClientRect()
    if (!r) return
    const below = window.innerHeight - r.bottom - 8
    const above = r.top > 300 && below < 200
    setDropStyle(above
      ? { position: 'fixed', left: r.left, top: r.top - 6, width: Math.max(r.width, 280), transform: 'translateY(-100%)', maxHeight: Math.max(160, Math.min(320, r.top - 8)) }
      : { position: 'fixed', left: r.left, top: r.bottom + 6, width: Math.max(r.width, 280), maxHeight: Math.max(160, Math.min(320, below)) })
  }, [])
  useEffect(() => {
    if (!open) { setDropStyle(null); return }
    syncDropPos()
    // 滚动可来自任意祖先容器 (capture), 滚动/缩放时保持贴位
    window.addEventListener('scroll', syncDropPos, true)
    window.addEventListener('resize', syncDropPos)
    return () => {
      window.removeEventListener('scroll', syncDropPos, true)
      window.removeEventListener('resize', syncDropPos)
    }
  }, [open, syncDropPos])

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      const t = e.target as Element
      // portal 下拉不在 containerRef 内, 须一并排除, 否则点选项先被判定外点而关闭
      if (t.closest?.('[data-suggest-drop]')) return
      if (containerRef.current && !containerRef.current.contains(t)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  // 键盘 active 项始终滚入可视区 (列表可长, 66+ 项)
  useEffect(() => {
    if (activeIdx < 0 || !listRef.current) return
    listRef.current.querySelectorAll('button[data-idx]').forEach(btn => {
      if (Number(btn.getAttribute('data-idx')) === activeIdx) btn.scrollIntoView({ block: 'nearest' })
    })
  }, [activeIdx])

  function handlePick(opt: SuggestOption) {
    onSelect(opt)
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || options.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, options.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const pick = options[activeIdx >= 0 ? activeIdx : 0]
      if (pick) handlePick(pick)
    }
  }

  const showList = open && dropStyle && (loading || options.length > 0 || value.trim().length > 0)
  return (
    <div ref={containerRef} className="relative">
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={e => { onChange(e.target.value); setOpen(true); setActiveIdx(0) }}
        onFocus={() => { setOpen(true); syncDropPos(); setActiveIdx(0) }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className={className}
      />
      {showList && createPortal(
        <div
          data-suggest-drop
          style={dropStyle}
          className="z-[70] flex animate-[sg-drop-in_120ms_ease-out] flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-2xl shadow-black/60 ring-1 ring-black/30"
        >
          {/* 结果计数 + 键盘操作提示 */}
          <div className="flex shrink-0 items-center justify-between border-b border-border/60 bg-elevated/40 px-3 py-1.5 text-[10px] text-muted">
            <span>{loading ? '搜索中…' : `${options.length} 个候选${value.trim() ? ` · “${value.trim()}”` : ''}`}</span>
            <span className="font-mono tracking-tight">↑↓ 导航 · ↵ 选中 · esc 关闭</span>
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1 [&::-webkit-scrollbar]:w-1 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border">
            {loading && options.length === 0 ? (
              <div className="space-y-1 px-2 py-1.5">
                {[0, 1, 2].map(i => <div key={i} className="h-9 animate-pulse rounded-lg bg-elevated/60" style={{ animationDelay: `${i * 120}ms` }} />)}
              </div>
            ) : options.length === 0 ? (
              <div className="px-3 py-6 text-center text-[11px] text-muted">
                无匹配候选 · 换个关键词或检查输入
              </div>
            ) : (
              options.map((opt, i) => (
                <button
                  key={opt.value}
                  data-idx={i}
                  type="button"
                  onClick={() => handlePick(opt)}
                  className={cn(
                    'relative mx-1 flex w-[calc(100%-8px)] flex-col gap-0.5 rounded-lg px-2.5 py-1.5 text-left transition-colors duration-75',
                    i === activeIdx ? 'bg-accent/10' : 'hover:bg-elevated/70',
                  )}
                >
                  {i === activeIdx && <span className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-accent" />}
                  <div className="flex min-w-0 items-center gap-1.5">
                    {opt.left && <span className="shrink-0 font-mono text-[11px] text-secondary">{opt.left}</span>}
                    <span className={cn('min-w-0 truncate text-xs', i === activeIdx ? 'text-accent' : 'text-foreground')}>{opt.label}</span>
                    {(opt.badges ?? []).slice(0, 3).map(b => (
                      <span key={b} className="shrink-0 rounded bg-elevated px-1 py-px text-[9px] leading-4 text-muted">{b}</span>
                    ))}
                    {opt.hint}
                  </div>
                  {(opt.desc || opt.value !== opt.label) && (
                    <div className="flex min-w-0 items-baseline gap-2">
                      {opt.desc && <span className="min-w-0 flex-1 truncate text-[10px] leading-4 text-muted">{opt.desc}</span>}
                      <span className="ml-auto shrink-0 font-mono text-[9px] text-muted/70">{opt.value}</span>
                    </div>
                  )}
                </button>
              ))
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

/** 下单面板 */
function OrderForm({ acc, holdings = [], onDone }: { acc: string; holdings?: PaperHolding[]; onDone: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('100')
  const [amount, setAmount] = useState('10000')
  const [qtyMode, setQtyMode] = useState<'qty' | 'amount'>('qty')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('market')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // 卖出时的持仓可卖提示 (T+1): 按代码匹配持仓行
  const sellPos = side === 'sell' ? holdings.find(h => h.symbol === symbol.trim().toUpperCase()) : undefined

  const m = useMutation({
    mutationFn: () =>
      api.paperOrderCreate({
        symbol: symbol.trim().toUpperCase(),
        side,
        ...(qtyMode === 'qty' ? { qty: Number(qty) } : { amount: Number(amount) }),
        order_type: orderType,
      }, acc),
    onSuccess: r => {
      setMsg({ ok: true, text: `已提交 ${ORDER_TYPE_LABEL[r.order.order_type]}单 ${r.order.side === 'buy' ? '买入' : '卖出'} ${r.order.symbol} x ${r.order.qty}` })
      onDone()
    },
    onError: e => setMsg({ ok: false, text: String((e as Error).message) }),
  })

  const canSubmit = symbol.trim().length >= 6
    && (qtyMode === 'qty' ? Number(qty) > 0 && Number(qty) % 100 === 0 : Number(amount) > 0)

  // 标的联想: 代码 / 中文名 / 拼音首字母, 与自选搜索同一后端 (ETF 可一并搜出)
  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbol.trim(), 'stock,etf'),
    queryFn: () => api.instrumentSearch(symbol.trim(), 20, 'stock,etf'),
    enabled: symbol.trim().length > 0,
    staleTime: 30_000,
  })
  const symbolOptions: SuggestOption[] = (symbolSearch.data?.results ?? []).map(r => {
    const b = boardTag(r.symbol)
    return {
      value: r.symbol,
      label: r.name || r.code || r.symbol,
      left: r.symbol,
      hint: b ? <span className={cn('shrink-0 rounded border px-1 py-px text-[9px] leading-none', b.color)}>{b.label}</span> : null,
    }
  })

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-sm font-medium">下单</div>
      <div className="mt-3 space-y-3">
        <div>
          <label className="text-[11px] text-muted">代码 (代码 / 中文名 / 拼音首字母联想)</label>
          <SuggestInput
            value={symbol}
            onChange={setSymbol}
            options={symbolOptions}
            onSelect={opt => setSymbol(opt.value)}
            loading={symbolSearch.isFetching}
            placeholder="600519.SH / 茅台 / MZ"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm uppercase outline-none focus:border-accent/50"
          />
          {side === 'sell' && (
            <div className="mt-1 text-[10px] text-muted">
              {sellPos
                ? `持有 ${sellPos.qty} 股 · T+1 可卖 ${sellPos.available_qty} 股 (含费用成本 ${fmtMoney(sellPos.avg_cost, 3)})`
                : symbol.trim() ? '该账户无此标的持仓' : '卖出前先选择持仓标的'}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          {(['buy', 'sell'] as const).map(s => (
            <button
              key={s}
              onClick={() => setSide(s)}
              className={cn(
                'flex-1 rounded-btn border py-1.5 text-xs font-medium transition-all duration-150 ease-smooth',
                side === s
                  ? s === 'buy'
                    ? 'border-bull/50 bg-bull/10 text-bull shadow-[0_0_0_1px_rgba(240,68,56,0.08)]'
                    : 'border-bear/50 bg-bear/10 text-bear shadow-[0_0_0_1px_rgba(18,183,106,0.08)]'
                  : 'border-border text-muted hover:border-border hover:bg-elevated/60 hover:text-secondary',
              )}
            >
              {s === 'buy' ? '买入' : '卖出'}
            </button>
          ))}
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[11px] text-muted">{qtyMode === 'qty' ? '数量 (股, 100 的整数倍)' : '金额 (元, 按最近收盘价折算)'}</label>
            <div className="flex gap-1 text-[10px]">
              {(['qty', 'amount'] as const).map(mo => (
                <button
                  key={mo}
                  onClick={() => setQtyMode(mo)}
                  className={cn('rounded px-1.5 py-px transition-colors', qtyMode === mo ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                >
                  {mo === 'qty' ? '按数量' : '按金额'}
                </button>
              ))}
            </div>
          </div>
          <input
            type="number"
            value={qtyMode === 'qty' ? qty : amount}
            onChange={e => (qtyMode === 'qty' ? setQty(e.target.value) : setAmount(e.target.value))}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        <div>
          <label className="text-[11px] text-muted">订单类型</label>
          <div className="mt-1 flex gap-1.5">
            {(Object.keys(ORDER_TYPE_LABEL) as Array<'market' | 'next_open' | 'close'>).map(t => (
              <button
                key={t}
                onClick={() => setOrderType(t)}
                title={t === 'market' ? '盘中按最新快照价成交; ETF 即时单自动转次日开盘' : undefined}
                className={cn(
                  'flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
                )}
              >
                {ORDER_TYPE_LABEL[t]}
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={() => canSubmit && m.mutate()}
          disabled={!canSubmit || m.isPending}
          className={cn(
            'w-full rounded-btn py-2 text-sm font-medium text-white transition-all duration-150 ease-smooth disabled:opacity-40 disabled:cursor-not-allowed',
            side === 'buy' ? 'bg-bull hover:bg-bull/85' : 'bg-bear hover:bg-bear/85',
          )}
        >
          {m.isPending ? '提交中…' : `${side === 'buy' ? '买入' : '卖出'} ${symbol.trim().toUpperCase() || ''}`}
        </button>
        {msg && (
          <div className={cn(
            'rounded-btn border px-2 py-1.5 text-[11px]',
            msg.ok
              ? side === 'buy' ? 'border-bull/20 bg-bull/[0.06] text-bull' : 'border-bear/20 bg-bear/[0.06] text-bear'
              : 'border-danger/20 bg-danger/[0.06] text-danger',
          )}>
            {msg.text}
          </div>
        )}
      </div>
    </div>
  )
}

/** 策略候选对比 (V3): 模拟盘统计 vs 回测候选 (回测报告的持久化标量摘要, 口径与回测对齐)。
 * 单位口径: 候选 metrics 为小数 (fmtPct 渲染), 模拟盘统计为百分数原值 — 各自按己方单位渲染, 不做数值换算。 */
interface PaperCompareStats {
  total_return_pct: number | null
  annual_pct: number | null
  max_drawdown: number | null
  win_rate: number | null
  n_trades: number | null
  profit_loss_ratio: number | null
}

function CandidateCompareCard({ paper }: { paper: PaperCompareStats }) {
  const candsQ = useQuery({ queryKey: QK.backtestCandidates, queryFn: api.backtestCandidates })
  const candidates = (candsQ.data?.items ?? []).filter(c => c.kind === 'strategy')
  const [selId, setSelId] = useState('')
  const sel = candidates.find(c => c.id === selId) ?? candidates[0]
  const m = sel?.metrics ?? {}
  const paperPct = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`)
  const candPct = (v: number | null | undefined) => (v == null ? '—' : fmtPct(v))
  const num = (v: number | null | undefined, digits = 0) => (v == null ? '—' : v.toFixed(digits))
  const rows: Array<[string, string, string]> = [
    ['累计收益', paperPct(paper.total_return_pct), candPct(m.total_return)],
    ['年化收益', paperPct(paper.annual_pct), candPct(m.annual_return)],
    ['最大回撤', paper.max_drawdown != null ? `-${paper.max_drawdown.toFixed(2)}%` : '—', candPct(m.max_drawdown)],
    ['胜率', paper.win_rate != null ? `${paper.win_rate.toFixed(1)}%` : '—', candPct(m.win_rate)],
    ['交易次数', num(paper.n_trades), num(m.n_trades)],
    ['盈亏比', num(paper.profit_loss_ratio, 2), num(m.profit_factor, 2)],
    ['夏普比率', '—', num(m.sharpe, 2)],
  ]
  return (
    <div className="rounded-card border border-border/60 bg-surface p-3">
      <div className="flex items-center gap-2">
        <div className="shrink-0 text-sm font-medium">策略对比</div>
        {candidates.length > 0 ? (
          <select
            value={sel?.id ?? ''}
            onChange={e => setSelId(e.target.value)}
            className="min-w-0 flex-1 rounded-btn border border-border bg-base px-1.5 py-1 text-xs text-secondary"
            title="选择要对比的策略候选"
          >
            {candidates.map(c => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        ) : (
          <span className="text-[10px] text-muted">暂无策略候选</span>
        )}
      </div>
      {sel ? (
        <table className="mt-2 w-full text-[11px]">
          <thead>
            <tr className="text-muted">
              <th className="py-0.5 text-left font-normal">指标</th>
              <th className="py-0.5 text-right font-normal">模拟盘</th>
              <th className="max-w-0 truncate py-0.5 text-right font-normal" title={sel.name}>{sel.name} (回测)</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map(([label, p, c]) => (
              <tr key={label} className="border-t border-border/40">
                <td className="py-1 font-sans text-muted">{label}</td>
                <td className="py-1 text-right text-secondary">{p}</td>
                <td className="py-1 text-right text-secondary">{c}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="mt-2 text-[11px] leading-relaxed text-muted">
          在策略页保存回测候选后, 可与模拟盘同口径对比 (累计/年化/回撤/胜率等)。
        </div>
      )}
    </div>
  )
}

/** 自动跟单规则列表 + 新建表单 (V2): 监控事件 → 自动下单 (按账户隔离) */
// ================================================================
// 费用与撮合设置 (当前账户弹窗): 佣金/印花税/滑点 + 涨跌停排队。
// 只影响之后的新成交, 不重算历史台账 (口径与回测费用模型一致)。
// ================================================================
function FeeSettingsModal({ accId, fees, queue, onSaved, onClose }: {
  accId: string
  fees: { commission_pct: number; stamp_tax_pct: number; slippage_bps: number }
  queue: boolean
  onSaved: () => void
  onClose: () => void
}) {
  const [commissionWan, setCommissionWan] = useState((fees.commission_pct * 10000).toFixed(2))
  const [stampQian, setStampQian] = useState((fees.stamp_tax_pct * 1000).toFixed(2))
  const [slippageBps, setSlippageBps] = useState(String(fees.slippage_bps))
  const [queueOn, setQueueOn] = useState(queue)
  const [err, setErr] = useState<string | null>(null)
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: () => api.paperSettings({
      commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
      stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
      slippage_bps: Number(slippageBps),
      queue_limit_orders: queueOn,
    }, accId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.paperAll })
      onSaved()
    },
    onError: e => setErr(String((e as Error).message)),
  })
  // 与后端 update_settings 的范围校验一致 (前端先行提示)
  const valid = Number(commissionWan) >= 0 && Number(commissionWan) <= 100
    && Number(stampQian) >= 0 && Number(stampQian) <= 50
    && Number(slippageBps) >= 0 && Number(slippageBps) <= 200
  const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  return (
    <Modal onClose={onClose} labelledBy="fee-settings-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <Settings className="h-4 w-4 text-accent" />
          <h3 id="fee-settings-title" className="text-sm font-semibold text-foreground">
            费用与撮合设置
            <span className="ml-1.5 font-mono text-[10px] text-muted">{accId}</span>
          </h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          仅影响之后的新成交, 已有台账与统计不重算。口径与回测费用模型一致。
        </p>
        <div className="mt-4 grid grid-cols-3 gap-2">
          <div>
            <label className="block text-[11px] text-muted">佣金 (万)</label>
            <input type="number" step="0.1" min="0" max="100" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">印花税 (千, 卖出)</label>
            <input type="number" step="0.1" min="0" max="50" value={stampQian} onChange={e => setStampQian(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">滑点 (bps)</label>
            <input type="number" step="1" min="0" max="200" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={inputCls} />
          </div>
        </div>
        <label className="mt-4 flex cursor-pointer items-center justify-between rounded-btn border border-border bg-base px-3 py-2">
          <span className="text-xs text-secondary">
            涨跌停排队
            <span className="ml-1 text-[10px] text-muted">触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)</span>
          </span>
          <input type="checkbox" checked={queueOn} onChange={e => setQueueOn(e.target.checked)} className="h-4 w-4 shrink-0" />
        </label>
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {m.isPending ? '保存中…' : '保存'}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
        {err && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{err}</div>}
      </div>
    </Modal>
  )
}

/** 模拟盘基础配置: 新建账户的默认初始资金与费率 (localStorage, 不落盘后端)。
 *  设置后创建表单 (单账户/批量) 优先加载它, 取代「记住上次取值」; 账户级费率
 *  仍可在各账户「费用与撮合设置」单独调整。 */
function BaseDefaultsModal({ onClose }: { onClose: () => void }) {
  const defaults = loadNewAccountDefaults()
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaults.stampQian)
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [saved, setSaved] = useState(false)
  const hasBase = (() => { try { return !!localStorage.getItem(PAPER_BASE_DEFAULTS_KEY) } catch { return false } })()
  const valid = Number(cash) > 0
    && Number(commissionWan) >= 0 && Number(commissionWan) <= 100
    && Number(stampQian) >= 0 && Number(stampQian) <= 50
    && Number(slippageBps) >= 0 && Number(slippageBps) <= 200
  const save = () => {
    try {
      localStorage.setItem(PAPER_BASE_DEFAULTS_KEY, JSON.stringify({ cash, commissionWan, stampQian, slippageBps }))
      setSaved(true)
      setTimeout(onClose, 600)
    } catch { /* 存储不可用时静默 */ }
  }
  const reset = () => {
    try { localStorage.removeItem(PAPER_BASE_DEFAULTS_KEY) } catch { /* 静默 */ }
    setCash('1000000'); setCommissionWan('2.5'); setStampQian('1'); setSlippageBps('5')
  }
  const inputCls = 'mt-1 w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  return (
    <Modal onClose={onClose} labelledBy="base-defaults-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-4 w-4 text-accent" />
          <h3 id="base-defaults-title" className="text-sm font-semibold text-foreground">基础配置 · 新账户默认</h3>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          新建账户 (单账户 / 批量) 自动加载以下默认值, 优先于「记住上次取值」;
          已有账户不受影响, 各账户费率仍可单独调整。
        </p>
        <label className="mt-4 block text-[11px] text-muted">初始虚拟资金 (元)</label>
        <input type="number" min="1" value={cash} onChange={e => setCash(e.target.value)} className={inputCls} />
        <div className="mt-3 grid grid-cols-3 gap-2">
          <div>
            <label className="block text-[11px] text-muted">佣金 (万)</label>
            <input type="number" step="0.1" min="0" max="100" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">印花税 (千, 卖出)</label>
            <input type="number" step="0.1" min="0" max="50" value={stampQian} onChange={e => setStampQian(e.target.value)} className={inputCls} />
          </div>
          <div>
            <label className="block text-[11px] text-muted">滑点 (bps)</label>
            <input type="number" step="1" min="0" max="200" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={inputCls} />
          </div>
        </div>
        <div className="mt-4 flex gap-2">
          <button
            onClick={save}
            disabled={!valid}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
          >
            {saved ? '已保存 ✓' : '保存为基础默认'}
          </button>
          {hasBase && (
            <button
              onClick={reset}
              className="rounded-btn border border-border px-3 text-xs text-muted transition-colors hover:border-warning/40 hover:text-warning"
              title="清除自定义基础配置, 回落内置默认 (100万 / 万2.5 / 千1 / 5bps)"
            >
              恢复内置默认
            </button>
          )}
          <button onClick={onClose} className="rounded-btn border border-border px-4 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ================================================================
// 删除账户强确认弹窗 (破坏性操作, per 设计规范必须二次确认):
// 展示将丢失的数据统计 + 显式勾选; 有持仓/待成交时后端先拒绝一次,
// 弹窗内呈现拒绝明细后再给「强制删除」(force) — 默认安全, 强删需两步。
// ================================================================
function DeleteAccountModal({ acc, name, holdingsCount, onClose, onDeleted }: {
  acc: string
  name?: string
  holdingsCount: number
  onClose: () => void
  onDeleted: () => void
}) {
  const [ack, setAck] = useState(false)
  const [forceHint, setForceHint] = useState<string | null>(null) // 后端拒绝明细 (有持仓/待成交)
  const [forceMode, setForceMode] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const m = useMutation({
    mutationFn: (force: boolean) => api.paperDeleteAccount(force, acc),
    onSuccess: onDeleted,
    onError: e => {
      const msg = String((e as Error).message)
      if (msg.includes('force')) {
        // 后端 fail-closed 拒绝: 显示明细并解锁强制删除路径
        setForceHint(msg)
      } else {
        setErr(msg)
      }
    },
  })
  return (
    <Modal onClose={onClose} labelledBy="del-acc-title">
      <div className="p-5">
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 text-danger" />
          <h3 id="del-acc-title" className="text-sm font-semibold text-foreground">
            删除账户 <span className="font-medium">{name || acc}</span>
          </h3>
          <span className="font-mono text-[10px] text-muted">{acc}</span>
        </div>
        <p className="mt-2 rounded-btn bg-danger/[0.06] px-2.5 py-2 text-[11px] leading-relaxed text-danger">
          将永久删除该账户的全部数据: 成交台账、持仓、净值曲线与跟单规则, <b>不可恢复</b>。
          {holdingsCount > 0 && <> 当前还有 {holdingsCount} 个持仓标的。</>}
        </p>
        <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs text-secondary">
          <input
            type="checkbox"
            checked={ack}
            onChange={e => setAck(e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 shrink-0"
          />
          我已知晓该账户数据将被永久删除且无法找回
        </label>
        {forceHint && (
          <div className="mt-3 rounded-btn border border-warning/30 bg-warning/[0.08] px-2.5 py-2 text-[11px] leading-relaxed text-warning">
            {forceHint}
            {!forceMode && (
              <button onClick={() => setForceMode(true)} className="ml-1 underline underline-offset-2">
                我仍要删除全部数据 →
              </button>
            )}
          </div>
        )}
        <div className="mt-4 flex gap-2">
          <button
            onClick={() => m.mutate(forceMode)}
            disabled={!ack || m.isPending || (!!forceHint && !forceMode)}
            className={cn(
              'flex-1 rounded-btn py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-40',
              forceMode ? 'bg-danger hover:bg-danger/85' : 'bg-danger/70 hover:bg-danger',
            )}
          >
            {m.isPending ? '删除中…' : forceMode ? '强制删除 (含持仓)' : '删除账户'}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">
            取消
          </button>
        </div>
        {err && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{err}</div>}
      </div>
    </Modal>
  )
}

// ================================================================
// 多账户横向对比: 归一化净值叠加图。
// ================================================================
function AutoRulesPanel({ acc }: { acc: string }) {
  const qc = useQueryClient()
  const rulesQ = useQuery({ queryKey: QK.paperAutoRules(acc), queryFn: () => api.paperAutoRules(acc) })
  const rules = rulesQ.data?.rules ?? []
  const [creating, setCreating] = useState(false)

  const invalidate = () => qc.invalidateQueries({ queryKey: QK.paperAutoRules(acc) })

  const toggleM = useMutation({
    mutationFn: (args: { id: string; enabled: boolean }) => api.paperAutoRuleSetEnabled(args.id, args.enabled, acc),
    onSuccess: invalidate,
  })
  const deleteM = useMutation({
    mutationFn: (id: string) => api.paperAutoRuleDelete(id, acc),
    onSuccess: invalidate,
  })

  if (creating) {
    return <AutoRuleForm acc={acc} onDone={() => { setCreating(false); invalidate() }} onCancel={() => setCreating(false)} />
  }
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">自动跟单规则</div>
        <button onClick={() => setCreating(true)} className="flex items-center gap-1 rounded-btn bg-accent/10 px-2.5 py-1 text-xs text-accent transition-colors hover:bg-accent/20">
          <Plus className="h-3 w-3" /> 新建规则
        </button>
      </div>
      <div className="mt-1 text-[11px] leading-relaxed text-muted">
        监控规则/策略触发时自动按规则下单 (默认次日开盘成交), 同标的 {`冷却 N 天`}内不重复触发
      </div>
      {rules.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted">暂无规则 — 新建一条, 让策略信号自动进入模拟盘</div>
      ) : (
        <div className="mt-2 space-y-1">
          {rules.map(r => (
            <div key={r.id} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs transition-colors hover:bg-elevated/40">
              <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', r.enabled ? 'bg-bear' : 'bg-muted')} />
              <span className="w-28 shrink-0 truncate font-medium">{r.name}</span>
              <span className="w-20 shrink-0 text-muted">{r.match_kind === 'strategy' ? '跟策略' : '跟规则'}</span>
              <span className="w-32 shrink-0 truncate font-mono text-[11px] text-muted" title={r.match_id}>{r.match_id}</span>
              <span className={cn('w-8 shrink-0 font-medium', r.side === 'buy' ? 'text-bull' : r.side === 'sell' ? 'text-bear' : 'text-accent')} title={r.side === 'auto' ? '自动: 入场信号买入 · 出场信号全平' : undefined}>{r.side === 'auto' ? '自动' : r.side === 'buy' ? '买' : '卖'}</span>
              <span className="w-24 shrink-0 font-mono text-[11px] text-muted">
                {r.size_mode === 'fixed_amount' ? fmtMoney(r.size_value, 0) : `${r.size_value}% 权益`}
              </span>
              <span className="w-16 shrink-0 text-[11px] text-muted">{ORDER_TYPE_LABEL[r.order_type]} · 冷却{r.cooldown_days}天</span>
              {r.max_positions != null && r.max_positions > 0 && (
                <span className="shrink-0 text-[11px] text-muted" title="最大同时持仓标的数 (0 = 不限)">≤{r.max_positions}仓</span>
              )}
              <button
                onClick={() => toggleM.mutate({ id: r.id, enabled: !r.enabled })}
                className={cn('ml-auto shrink-0 rounded-btn px-2 py-0.5 text-[10px] transition-colors',
                  r.enabled ? 'bg-bear/10 text-bear hover:bg-bear/20' : 'bg-elevated text-muted hover:text-secondary')}
              >
                {r.enabled ? '停用' : '启用'}
              </button>
              <button onClick={() => deleteM.mutate(r.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="删除规则">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AutoRuleForm({ acc, onDone, onCancel }: { acc: string; onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState('')
  const [matchKind, setMatchKind] = useState<'strategy' | 'rule'>('strategy')
  const [matchId, setMatchId] = useState('')
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('fixed_amount')
  const [sizeValue, setSizeValue] = useState('10000')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')
  const [msg, setMsg] = useState<string | null>(null)

  const m = useMutation({
    mutationFn: () =>
      api.paperAutoRuleCreate({
        name: name.trim(),
        match_kind: matchKind,
        match_id: matchId.trim(),
        side,
        size_mode: sizeMode,
        size_value: Number(sizeValue),
        order_type: orderType,
        cooldown_days: Number(cooldown),
        max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
        enabled: true,
      }, acc),
    onSuccess: onDone,
    onError: e => setMsg(String((e as Error).message)),
  })

  const valid = name.trim() && matchId.trim() && Number(sizeValue) > 0

  // ID 联想: 跟策略 → 策略引擎列表; 跟监控规则 → 监控规则列表。本地按中文名/ID 过滤。
  const strategiesQ = useQuery({
    // 与 Screener/DefaultStrategyParamsDialog 同键同请求 (api.screenerStrategies(
    // undefined, 'all')), 共享缓存而不是各拼哨兵值各拉一份
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({
    queryKey: QK.monitorRules,
    queryFn: api.monitorRulesList,
    staleTime: 30_000,
  })
  const idQuery = matchId.trim().toLowerCase()
  const idOptions: SuggestOption[] = (() => {
    if (matchKind === 'strategy') {
      return selectableStrategies(strategiesQ.data?.presets ?? [], [matchId])
        .filter(s => !idQuery || s.name.toLowerCase().includes(idQuery) || s.id.toLowerCase().includes(idQuery))
        .slice(0, 8)
        .map(s => ({
          value: s.id,
          label: strategyOptionLabel(s),
          hint: <span className="shrink-0 font-mono text-[10px] text-muted">{s.id}</span>,
        }))
    }
    return (rulesQ.data?.rules ?? [])
      .filter(r => !idQuery || r.name.toLowerCase().includes(idQuery) || r.id.toLowerCase().includes(idQuery))
      .slice(0, 8)
      .map(r => ({
        value: r.id,
        label: r.name || r.id,
        hint: <span className={cn('shrink-0 font-mono text-[10px]', r.enabled ? 'text-muted' : 'text-warning/70')}>{r.id}</span>,
      }))
  })()

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-sm font-medium">新建自动跟单规则</div>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <label className="text-[11px] text-muted">规则名称</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="如: 跟单前高突破"
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted">跟什么</label>
          <div className="mt-1 flex gap-1.5">
            {(['strategy', 'rule'] as const).map(k => (
              <button key={k} onClick={() => { setMatchKind(k); setMatchId(''); setSide(s => (k === 'strategy' ? 'auto' : s === 'auto' ? 'buy' : s)) }}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  matchKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                {k === 'strategy' ? '跟策略' : '跟监控规则'}
              </button>
            ))}
          </div>
        </div>
        <div className="md:col-span-2">
          <label className="text-[11px] text-muted">{matchKind === 'strategy' ? '策略 (输入中文名 / ID 联想, 留空聚焦看全部)' : '监控规则 (输入名称 / ID 联想)'}</label>
          <SuggestInput
            value={matchId}
            onChange={setMatchId}
            options={idOptions}
            onSelect={opt => setMatchId(opt.value)}
            loading={matchKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
            placeholder={matchKind === 'strategy' ? '如: 前高突破 / sg_...' : '如: 高位放量 / mrule_...'}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50"
          />
        </div>
        {matchKind === 'strategy' ? (
          <div className="self-end pb-1.5 text-[10px] leading-4 text-muted">
            入场信号买入 · 出场信号全平
          </div>
        ) : (
          <div>
            <label className="text-[11px] text-muted">方向</label>
            <div className="mt-1 flex gap-1.5">
              {(['buy', 'sell'] as const).map(sd => (
                <button key={sd} onClick={() => setSide(sd)}
                  className={cn('flex-1 whitespace-nowrap rounded-btn border py-1 text-[11px] font-medium transition-colors',
                    side === sd ? (sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted')}>
                  {sd === 'buy' ? '买入' : '卖出'}
                </button>
              ))}
            </div>
          </div>
        )}
        <div>
          <label className="text-[11px] text-muted">仓位</label>
          <div className="mt-1 flex gap-1.5">
            <button onClick={() => setSizeMode('fixed_amount')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
            <button onClick={() => setSizeMode('pct_equity')}
              className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted">{sizeMode === 'fixed_amount' ? '每笔金额 (元)' : '每笔占权益 %'}</label>
          <input type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted">订单类型</label>
          <div className="mt-1 flex gap-1.5">
            {(['next_open', 'close', 'market'] as const).map(t => (
              <button key={t} onClick={() => setOrderType(t)}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors',
                  orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                {ORDER_TYPE_LABEL[t]}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="text-[11px] text-muted">冷却天数 (同标的)</label>
          <input type="number" value={cooldown} onChange={e => setCooldown(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
        <div>
          <label className="text-[11px] text-muted" title="最多同时持有的标的数 (买入开新仓受限, 0 = 不限) — 与回测 max_positions 同口径">最大持仓 (标的数)</label>
          <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)}
            className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent/50" />
        </div>
      </div>
      <div className="mt-3 flex gap-2">
        <button onClick={() => valid && m.mutate()} disabled={!valid || m.isPending}
          className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40">
          {m.isPending ? '创建中…' : '创建规则'}
        </button>
        <button onClick={onCancel} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:text-foreground">取消</button>
      </div>
      {msg && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{msg}</div>}
    </div>
  )
}

// ================================================================
// 多账户对比 (V3): 模拟盘默认主视图 — 对比优先, 收益降序。
// ================================================================

type CmpSortKey = 'name' | 'total' | 'pnl_pct' | 'day_change_pct' | 'win_rate' | 'profit_loss_ratio' | 'rounds' | 'holdings_count' | 'last_nav_date' | 'realized_pnl' | 'max_drawdown'

/** 空值判定: 这些列无值行恒垫底, 不随排序方向翻到顶部 */
const CMP_NULLABLE: Partial<Record<CmpSortKey, (r: PaperCompareRow) => boolean>> = {
  max_drawdown: r => r.max_drawdown == null,
  profit_loss_ratio: r => r.profit_loss_ratio == null,
  day_change_pct: r => r.day_change_pct == null,
  realized_pnl: r => r.realized_pnl == null,
  last_nav_date: r => !r.last_nav_date,
}

/** 排序取值 */
function cmpSortVal(k: CmpSortKey, r: PaperCompareRow): string | number {
  switch (k) {
    case 'name': return r.name
    case 'total': return r.total ?? 0
    case 'pnl_pct': return r.pnl_pct ?? 0
    case 'day_change_pct': return r.day_change_pct ?? 0
    case 'win_rate': return r.win_rate ?? 0
    case 'profit_loss_ratio': return r.profit_loss_ratio ?? 0
    case 'rounds': return r.rounds ?? 0
    case 'holdings_count': return r.holdings_count ?? 0
    case 'last_nav_date': return r.last_nav_date ?? ''
    case 'realized_pnl': return r.realized_pnl ?? 0
    case 'max_drawdown': return -(r.max_drawdown ?? 0) // 回撤绝对值越小越优 → 取负后降序=回撤小在前
  }
}

/** 可排序表头: 点击切换该列升降序, 活动列显方向箭头, 非活动列 hover 显淡箭头 (样式随 Review._DtTh) */
const CMP_SORT_OPTIONS: Array<{ key: CmpSortKey; label: string }> = [
  { key: 'pnl_pct', label: '累计收益' },
  { key: 'total', label: '总资产' },
  { key: 'day_change_pct', label: '最新日' },
  { key: 'realized_pnl', label: '已实现' },
  { key: 'max_drawdown', label: '回撤' },
  { key: 'win_rate', label: '胜率' },
  { key: 'profit_loss_ratio', label: '盈亏比' },
  { key: 'rounds', label: '回合' },
  { key: 'holdings_count', label: '持仓' },
  { key: 'last_nav_date', label: '最近结算' },
  { key: 'name', label: '账户名' },
]

function CompareView({ onCreateSingle }: { onCreateSingle: () => void }) {
  const [createOpen, setCreateOpen] = useState(false)
  const [baseOpen, setBaseOpen] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  // 资金全景右侧聚焦账户 (与终端展开解耦: 关掉终端仍保留聚焦)
  const [focusAcc, setFocusAcc] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<PaperCompareRow | null>(null)
  const qc = useQueryClient()
  // 卡片排序: 默认累计收益降序; 换列时数值列默认降序、账户名默认升序
  const [sort, setSort] = useState<{ key: CmpSortKey; desc: boolean }>({ key: 'pnl_pct', desc: true })
  // 60s 轻轮询: 盘中自动跟单下单 / 盘后结算后榜单自然刷新 (保留旧数据防闪烁)。
  // 非交易时段榜单不变, 降为 5 分钟兜底 (字段缺失保持 60s)
  const { data: quoteStatus } = useQuoteStatus()
  const cmpQ = useQuery({
    queryKey: QK.paperCompare,
    queryFn: api.paperCompare,
    refetchInterval: () => (quoteStatus?.is_trading_hours === false ? 300_000 : 60_000),
    placeholderData: (prev: any) => prev,
  })
  // 跨账户实时撮合流水: 盘中 5s 轮询 (后端 3s 共享缓存), 休市 30s 兜底
  const feedQ = useQuery({
    queryKey: ['paper-live-feed'],
    queryFn: () => api.paperLiveFeed(60),
    refetchInterval: () => (quoteStatus?.is_trading_hours === false ? 30_000 : 5_000),
    placeholderData: (prev: any) => prev,
    staleTime: 3_000,
  })

  const rows = cmpQ.data?.accounts ?? []
  // 最近有真实委托 / 仍在撮合的账户 → 卡片上显示 LIVE / 撮合中脉冲
  const liveAccounts = useMemo(
    () => new Set((feedQ.data?.items ?? []).map(it => it.account)),
    [feedQ.data],
  )
  const matchingAccounts = useMemo(
    () => new Set((feedQ.data?.items ?? []).filter(it => it.status === 'pending').map(it => it.account)),
    [feedQ.data],
  )
  const executionQ = useQuery({ queryKey: executionListKey, queryFn: executionApi.list, enabled: rows.length > 0, refetchInterval: 60_000, retry: false })
  const executionByAccount = new Map(executionQ.data?.bindings.map(b => [b.account, b]))
  // 排序后的账户列表 (节点条 / 资金全景右侧账户共用)
  const sorted = useMemo(() => {
    const dir = sort.desc ? -1 : 1
    const isNull = CMP_NULLABLE[sort.key]
    return [...rows].sort((a, b) => {
      if (isNull) {
        const na = isNull(a), nb = isNull(b)
        if (na || nb) return na === nb ? 0 : na ? 1 : -1 // 无值行恒垫底, 不随方向翻到顶部
      }
      const va = cmpSortVal(sort.key, a)
      const vb = cmpSortVal(sort.key, b)
      const c = typeof va === 'string' || typeof vb === 'string'
        ? String(va).localeCompare(String(vb), 'zh-Hans-CN')
        : va - vb
      return c * dir
    })
  }, [rows, sort])
  const avgPct = rows.length > 0
    ? rows.reduce((s, r) => s + (r.pnl_pct ?? 0), 0) / rows.length
    : null
  const totalAssets = rows.reduce((s, r) => s + (r.total ?? 0), 0)
  const dayPcts = rows.map(r => r.day_change_pct).filter((v): v is number => v != null)
  const avgDay = dayPcts.length > 0 ? dayPcts.reduce((s, v) => s + v, 0) / dayPcts.length : null
  const totalHoldings = rows.reduce((s, r) => s + (r.holdings_count ?? 0), 0)
  const lastSettle = rows.map(r => r.last_nav_date).filter(Boolean).sort().at(-1) ?? null
  const selectedRow = sorted.find(r => r.account === expanded) ?? null
  const focused = sorted.find(r => r.account === (focusAcc ?? expanded ?? feedQ.data?.items[0]?.account)) ?? sorted[0] ?? null
  const feedItems = feedQ.data?.items ?? []
  // 左栏: 真实成交回报聚合出的标的流 (同一标的按笔数排序)
  const symbols = useMemo<FlowSymbol[]>(() => {
    const map = new Map<string, FlowSymbol>()
    for (const it of feedItems) {
      const cur = map.get(it.symbol)
      if (!cur) {
        map.set(it.symbol, {
          symbol: it.symbol,
          name: it.symbol_name || '',
          price: it.last_price ?? it.fill_price ?? null,
          changePct: it.change_pct ?? null,
          side: it.side,
          count: 1,
        })
      } else {
        cur.count += 1
        if (it.last_price != null) cur.price = it.last_price
        else if (it.fill_price != null) cur.price = it.fill_price
        if (it.change_pct != null) cur.changePct = it.change_pct
        if (it.symbol_name) cur.name = it.symbol_name
        cur.side = it.side
      }
    }
    return [...map.values()].sort((a, b) => b.count - a.count).slice(0, 32)
  }, [feedQ.data])
  // 中栏: 参数跟单绑定 + 各账户已绑定的自动跟单规则 (真实策略库)
  const strategies = useMemo<FlowStrategy[]>(() => {
    const out: FlowStrategy[] = []
    for (const [acc, b] of executionByAccount) {
      const owner = rows.find(r => r.account === acc)
      out.push({
        id: `${acc}:exec`,
        name: b.name,
        account: acc,
        accountName: owner?.name ?? acc,
        enabled: b.enabled,
        kind: 'strategy',
        side: 'buy',
      })
    }
    for (const r of sorted) {
      for (const ar of r.auto_rules ?? []) {
        out.push({
          id: `${r.account}:${ar.match_kind}:${ar.match_id}`,
          name: ar.name,
          account: r.account,
          accountName: r.name,
          enabled: ar.enabled,
          kind: ar.match_kind,
          side: ar.side,
        })
      }
    }
    return out
  }, [sorted, executionQ.data])  // 终端渲染在编队下方: 打开后自动滚到终端, 避免「点了没反应」的错觉。
  // 账户面板的行情/订单请求约 1s 才回, 内容撑高后需要再滚一次才能对齐顶部。
  const terminalRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!expanded) return
    const smooth = !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const scroll = () => terminalRef.current?.scrollIntoView({
      block: 'start',
      behavior: smooth ? 'smooth' : 'auto',
    })
    const timers = [180, 700, 1500].map(ms => window.setTimeout(scroll, ms))
    return () => timers.forEach(t => window.clearTimeout(t))
  }, [expanded])

  return (
    <div className="tf-paper flex min-h-0 flex-1 flex-col">
      <PageHeader
        title="模拟盘"
        subtitle="资金全景 — 行情 → 策略 → 账户, 实时资本流向"
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:bg-accent/90"
              title="同本金同费率批量创建一批账户, 各绑一条策略/监控规则跟单, 保证对比口径一致"
            >
              <Plus className="h-3 w-3" />
              批量创建账户
            </button>
            <button
              onClick={onCreateSingle}
              className="flex items-center gap-0.5 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="新建单个虚拟账户 (原流程)"
            >
              <Plus className="h-3 w-3" />
              单账户
            </button>
            <button
              onClick={() => setBaseOpen(true)}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
              title="新建账户的默认初始资金与费率 (佣金/印花税/滑点), 创建时自动加载"
            >
              <SlidersHorizontal className="h-3 w-3" />
              基础配置
            </button>
            <button
              onClick={() => cmpQ.refetch()}
              className="flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
            >
              <RefreshCw className={cn('h-3 w-3', cmpQ.isFetching && 'animate-spin')} />
              刷新
            </button>
          </div>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {cmpQ.isLoading ? (
          <div className="py-16 text-center text-sm text-muted">加载中…</div>
        ) : cmpQ.isError ? (
          <div className="py-16 text-center text-sm text-danger">
            对比数据加载失败
            <button onClick={() => cmpQ.refetch()} className="ml-2 rounded-btn border border-border px-2 py-0.5 text-xs text-secondary hover:text-foreground">重试</button>
          </div>
        ) : rows.length === 0 ? (
          <div className="mx-auto mt-16 max-w-md rounded-card border border-dashed border-border bg-surface/50 px-6 py-14 text-center">
            <GitCompare className="mx-auto h-8 w-8 text-muted/50" />
            <div className="mt-3 text-sm font-medium">还没有对比账户</div>
            <div className="mt-1.5 text-xs leading-relaxed text-muted">
              同本金、同费率批量创建一批账户, 各绑一个策略或监控规则,
              信号自动跟单 — 同口径的并行对比才有参考意义。
            </div>
            <div className="mt-4 flex justify-center gap-2">
              <button
                onClick={() => setCreateOpen(true)}
                className="flex items-center gap-1 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:bg-accent/90"
              >
                <Plus className="h-3.5 w-3.5" />
                批量创建账户
              </button>
              <button
                onClick={onCreateSingle}
                className="rounded-btn border border-border px-3 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
              >
                先建一个单账户
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {/* ===== 指挥台: 紧凑数据头 ===== */}
            <div className="tf-desk">
              <span className="tf-desk-grid" aria-hidden="true" />
              <span className="tf-desk-beacon" aria-hidden="true" />
              <div className="relative flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="tf-live-dot" aria-hidden="true" />
                <span className="font-mono text-[10px] tracking-[0.26em] text-muted">PAPER ARENA · SIM DESK</span>
                <DeskClock />
                <span className={cn('tf-engine-state', rows.length > 0 && 'is-running')}>
                  {rows.length > 0 ? '引擎在线' : '待接入'}
                </span>
                <div className="ml-auto flex items-center gap-1.5">
                  <span className="hidden text-[10px] text-muted sm:inline">排序</span>
                  <select
                    value={sort.key}
                    onChange={e => {
                      const key = e.target.value as CmpSortKey
                      setSort(s => ({ key, desc: key === 'name' ? false : s.key === key ? s.desc : true }))
                    }}
                    className="rounded-btn border border-border bg-elevated/60 px-2 py-1 text-[11px] text-secondary outline-none transition-colors hover:border-accent/40 focus:border-accent/60"
                    title="排序字段"
                  >
                    {CMP_SORT_OPTIONS.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
                  </select>
                  <button
                    type="button"
                    onClick={() => setSort(s => ({ ...s, desc: !s.desc }))}
                    className="rounded-btn border border-border p-1 text-muted transition-colors hover:border-accent/40 hover:text-accent"
                    title={sort.desc ? '当前降序, 点击升序' : '当前升序, 点击降序'}
                  >
                    {sort.desc ? <ChevronDown className="h-3 w-3" /> : <ChevronUp className="h-3 w-3" />}
                  </button>
                </div>
              </div>
              <div className="relative mt-2 flex flex-wrap items-end gap-x-7 gap-y-2">
                <div>
                  <div className="text-[9px] tracking-[0.18em] text-muted">TOTAL EQUITY</div>
                  <div className="mt-0.5 flex items-baseline gap-2">
                    <AnimatedNumber
                      value={totalAssets}
                      format={v => fmtMoney(v, 0)}
                      resetKey={lastSettle ?? undefined}
                      className="font-mono text-[30px] font-semibold leading-none tabular tracking-tight"
                    />
                    <span className={cn('font-mono text-xs tabular', priceColorClass((avgPct ?? 0) / 100))}>
                      {avgPct == null ? '—' : fmtPct(avgPct / 100)}
                    </span>
                    <span className="text-[9px] text-muted">平均累计</span>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
                  {[
                    { k: '编队', v: String(rows.length) },
                    { k: '持仓', v: String(totalHoldings) },
                    { k: '平均日涨跌', v: avgDay == null ? '—' : fmtPct(avgDay / 100), cls: avgDay == null ? '' : priceColorClass(avgDay / 100) },
                    { k: '最近结算', v: lastSettle ?? '—' },
                  ].map(it => (
                    <div key={it.k} className="min-w-[72px]">
                      <div className="text-[9px] text-muted">{it.k}</div>
                      <div className={cn('font-mono text-sm tabular text-secondary', it.cls)}>{it.v}</div>
                    </div>
                  ))}
                </div>
                <PulseLine live className="ml-auto h-5 w-24 opacity-70" />
              </div>
            </div>

            {/* ===== 账户节点条: 无缝滚动, 点击聚焦/展开 ===== */}
            <div className="tf-node-strip">
              <span className="tf-node-strip-tag">NODES</span>
              <div className="tf-node-rail">
                <div className="tf-node-track">
                  {[...sorted, ...sorted].map((r, i) => {
                    const pct = r.estimating ? (r.live_pnl_pct ?? r.pnl_pct ?? 0) : (r.pnl_pct ?? 0)
                    const active = focused?.account === r.account
                    return (
                      <button
                        key={`${r.account}-${i}`}
                        type="button"
                        onClick={() => {
                          setFocusAcc(r.account)
                          setExpanded(r.account)
                        }}
                        className={cn('tf-node-chip', active && 'is-active', r.status === 'frozen' && 'is-frozen')}
                        title={`${r.name} · ${r.account} · 累计 ${fmtPct(pct / 100)}`}
                      >
                        <span className="tf-node-rank">{sorted.length ? (i % sorted.length) + 1 : 1}</span>
                        {r.market && r.market !== 'cn' && <span className="tf-node-mkt">{PNL_MKT_LABEL[r.market]}</span>}
                        <span className="max-w-24 truncate">{r.name}</span>
                        <b className={cn('font-mono tabular', priceColorClass(pct / 100))}>{fmtPct(pct / 100)}</b>
                        {(matchingAccounts.has(r.account) || liveAccounts.has(r.account)) && (
                          <span
                            className={cn('tf-node-live', matchingAccounts.has(r.account) && 'is-matching')}
                            aria-hidden="true"
                          />
                        )}
                      </button>
                    )
                  })}
                </div>
              </div>
            </div>

            <PnlBoard
              rows={sorted}
              onOpen={r => {
                setFocusAcc(r.account)
                setExpanded(r.account)
              }}
            />

            <CapitalFlowStage
              symbols={symbols}
              strategies={strategies}
              accounts={sorted}
              focusAccount={focused}
              orders={feedItems}
              totals={feedQ.data?.totals}
              onSelectAccount={acc => setFocusAcc(acc)}
              onOpenTerminal={acc => {
                setFocusAcc(acc.account)
                setExpanded(acc.account)
              }}
            />

            {selectedRow && (
              <div ref={terminalRef} className="tf-terminal scroll-mt-24">
                <div className="tf-terminal-bar">
                  <span className="tf-live-dot" aria-hidden="true" />
                  <span className="font-mono text-[10px] tracking-[0.22em] text-muted">EXECUTION TERMINAL</span>
                  <span className="h-3 w-px bg-border" aria-hidden="true" />
                  <span className="truncate text-sm font-medium">{selectedRow.name}</span>
                  <span className="hidden truncate font-mono text-[10px] text-muted sm:inline">{selectedRow.account}</span>
                  <button
                    onClick={() => setExpanded(null)}
                    className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-danger/40 hover:text-danger"
                  >
                    <X className="h-3 w-3" />
                    关闭终端
                  </button>
                </div>
                <div className="p-4">
                  <AccountPanel acc={selectedRow.account} name={selectedRow.name} onDeleted={() => setExpanded(null)} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
      {createOpen && <BatchCreateModal onClose={() => setCreateOpen(false)} onCreated={() => setCreateOpen(false)} />}
      {baseOpen && <BaseDefaultsModal onClose={() => setBaseOpen(false)} />}
      {deleting && (
        <DeleteAccountModal
          acc={deleting.account}
          name={deleting.name}
          holdingsCount={deleting.holdings_count ?? 0}
          onClose={() => setDeleting(null)}
          onDeleted={() => {
            setDeleting(null)
            setExpanded(null)
            qc.invalidateQueries({ queryKey: QK.paperAll })
          }}
        />
      )}
    </div>
  )
}

// ================================================================
// 批量创建对比账户弹窗: 同口径由构造保证 (同本金/同费率/同跟单参数)。
// 来源行只带 名称+匹配目标; 方向/仓位/订单类型/冷却为全表共享参数。
// ================================================================
type BatchSourceRow = { name: string; kind: 'strategy' | 'rule'; matchId: string }

function BatchCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (count: number) => void }) {
  const qc = useQueryClient()
  const defaults = loadNewAccountDefaults()
  const { market: globalMarket } = useMarket()
  const [market, setMarket] = useState<Market>(globalMarket)
  const [cash, setCash] = useState(defaults.cash)
  const [commissionWan, setCommissionWan] = useState(defaults.commissionWan)
  const [stampQian, setStampQian] = useState(defaultStampQian(globalMarket, defaults.stampQian))
  const [slippageBps, setSlippageBps] = useState(defaults.slippageBps)
  const [prefix, setPrefix] = useState('')
  const [rows, setRows] = useState<BatchSourceRow[]>([])
  const [quickKind, setQuickKind] = useState<'strategy' | 'rule'>('strategy')
  const [quickId, setQuickId] = useState('')
  // 共享跟单参数 (应用到全部来源 → 同规格对比)
  const [side, setSide] = useState<'buy' | 'sell' | 'auto'>('auto')
  const [sizeMode, setSizeMode] = useState<'fixed_amount' | 'pct_equity'>('pct_equity')
  const [sizeValue, setSizeValue] = useState('10')
  const [orderType, setOrderType] = useState<'market' | 'next_open' | 'close'>('next_open')
  const [cooldown, setCooldown] = useState('5')
  const [maxPositions, setMaxPositions] = useState('10')

  const strategiesQ = useQuery({
    // 与 Screener/DefaultStrategyParamsDialog 同键同请求 (api.screenerStrategies(
    // undefined, 'all')), 共享缓存而不是各拼哨兵值各拉一份
    queryKey: [...QK.screenerStrategies('all'), 'all'],
    queryFn: () => api.screenerStrategies(undefined, 'all'),
    staleTime: 60_000,
  })
  const rulesQ = useQuery({ queryKey: QK.monitorRules, queryFn: api.monitorRulesList, staleTime: 30_000 })

  const m = useMutation({
    mutationFn: () =>
      api.paperArenaCreate({
        initial_cash: Number(cash),
        name_prefix: prefix.trim() || undefined,
        market,
        commission_pct: Number(commissionWan) / 10000,   // 万 X → pct
        stamp_tax_pct: Number(stampQian) / 1000,         // 千 X → pct
        slippage_bps: Number(slippageBps),
        sources: rows.map(r => ({
          name: r.name.trim() || undefined,
          match_kind: r.kind,
          match_id: r.matchId.trim(),
          side,
          size_mode: sizeMode,
          size_value: Number(sizeValue),
          order_type: orderType,
          cooldown_days: Number(cooldown),
          max_positions: Math.max(0, Math.round(Number(maxPositions) || 0)),
        })),
      }),
    onSuccess: r => {
      qc.invalidateQueries({ queryKey: QK.paperAll })
      try {
        localStorage.setItem(NEW_ACCOUNT_DEFAULTS_KEY, JSON.stringify({ cash, commissionWan, stampQian, slippageBps }))
      } catch { /* 存储不可用时静默 */ }
      onCreated(r.created.length)
    },
  })

  const addRow = (row: BatchSourceRow) => {
    setRows(prev => (prev.some(r => r.matchId === row.matchId) ? prev : [...prev, row]))
    // 混入监控规则来源后 auto 不再合法 (后端校验拒绝), 落回买入
    if (row.kind === 'rule') setSide(s => (s === 'auto' ? 'buy' : s))
  }
  const addAllStrategies = () => {
    const presets = selectableStrategies(strategiesQ.data?.presets ?? [])
    setRows(prev => {
      const seen = new Set(prev.map(r => r.matchId))
      return [...prev, ...presets.filter(p => !seen.has(p.id)).map(p => ({ name: p.name || p.id, kind: 'strategy' as const, matchId: p.id }))]
    })
  }

  const valid = rows.length >= 1
    && rows.every(r => r.matchId.trim().length > 0)
    && Number(cash) > 0
    && Number(sizeValue) > 0
    && Number(commissionWan) >= 0 && Number(stampQian) >= 0 && Number(slippageBps) >= 0

  const quickQuery = quickId.trim().toLowerCase()
  const quickOptions: SuggestOption[] = quickKind === 'strategy'
    ? selectableStrategies(strategiesQ.data?.presets ?? [], [quickId])
        .filter(s => !quickQuery || s.name.toLowerCase().includes(quickQuery) || s.id.toLowerCase().includes(quickQuery) || (s.tags ?? []).some(t => t.toLowerCase().includes(quickQuery)))
        .map(s => ({
          value: s.id,
          label: strategyOptionLabel(s),
          desc: s.description || undefined,
          badges: [
            ...(s.library?.group_name ? [s.library.group_name] : []),
            ...(s.timeframes ?? []).map(tf => tf === '1d' ? '日线' : tf === '1m' ? '分钟' : tf),
            ...(s.tags ?? []).slice(0, 2),
          ].slice(0, 3),
        }))
    : (rulesQ.data?.rules ?? [])
        .filter(r => !quickQuery || r.name.toLowerCase().includes(quickQuery) || r.id.toLowerCase().includes(quickQuery))
        .map(r => ({ value: r.id, label: r.name || r.id }))

  const inputCls = 'w-full rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm outline-none focus:border-accent/50'
  const labelCls = 'block text-[11px] text-muted'

  return (
    <Modal onClose={onClose} labelledBy="arena-create-title" panelClassName="w-[94vw] max-w-3xl bg-surface border border-border rounded-card shadow-xl">
      <div className="max-h-[86vh] overflow-y-auto p-5">
        <div className="flex items-center gap-2">
          <GitCompare className="h-4 w-4 text-accent" />
          <h3 id="arena-create-title" className="text-sm font-semibold text-foreground">批量创建对比账户（旧事件模式）</h3>
          <span className="text-[10px] text-muted">同本金 · 同费率 · 同跟单参数 — 口径一致由构造保证</span>
        </div>

        {/* 对比来源 */}
        <div className="mt-4 rounded-card border border-border bg-base/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex gap-1">
              {(['strategy', 'rule'] as const).map(k => (
                <button key={k} onClick={() => { setQuickKind(k); setQuickId('') }}
                  className={cn('rounded-btn border px-2 py-1 text-[11px] transition-colors',
                    quickKind === k ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {k === 'strategy' ? '策略' : '监控规则'}
                </button>
              ))}
            </div>
            <div className="min-w-52 flex-1">
              <SuggestInput
                value={quickId}
                onChange={setQuickId}
                options={quickOptions}
                onSelect={opt => {
                  addRow({ name: opt.label, kind: quickKind, matchId: opt.value })
                  setQuickId('')
                }}
                loading={quickKind === 'strategy' ? strategiesQ.isPending : rulesQ.isPending}
                placeholder={quickKind === 'strategy' ? '搜索策略加入对比 (中文名 / ID)' : '搜索监控规则加入对比'}
                className="w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
              />
            </div>
            <button
              onClick={() => { const opt = quickOptions[0]; if (opt) { addRow({ name: opt.label, kind: quickKind, matchId: opt.value }); setQuickId('') } }}
              disabled={quickOptions.length === 0}
              className="rounded-btn bg-accent/10 px-2.5 py-1 text-[11px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-40"
            >
              添加
            </button>
            {quickKind === 'strategy' && (
              <button
                onClick={addAllStrategies}
                disabled={selectableStrategies(strategiesQ.data?.presets ?? []).length === 0}
                className="rounded-btn border border-border px-2.5 py-1 text-[11px] text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40"
                title="把现有策略各开一个账户 (单次最多 60 个)"
              >
                全部策略一键加入 ({selectableStrategies(strategiesQ.data?.presets ?? []).length})
              </button>
            )}
          </div>

          {rows.length === 0 ? (
            <div className="py-6 text-center text-[11px] text-muted">还没有来源 — 上面搜索添加, 或「全部策略一键加入」</div>
          ) : (
            <div className="mt-2 max-h-52 space-y-1 overflow-y-auto">
              {rows.map((r, i) => (
                <div key={r.matchId} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/40">
                  <span className="w-16 shrink-0 text-[10px] text-muted">{r.kind === 'strategy' ? '跟策略' : '跟规则'}</span>
                  <input
                    value={r.name}
                    onChange={e => setRows(prev => prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                    placeholder="账户名 (缺省用 ID)"
                    className="min-w-0 flex-1 rounded-btn border border-border bg-base px-2 py-1 text-xs outline-none focus:border-accent/50"
                  />
                  <span className="w-44 shrink-0 truncate font-mono text-[11px] text-muted" title={r.matchId}>{r.matchId}</span>
                  <button onClick={() => setRows(prev => prev.filter((_, j) => j !== i))} className="shrink-0 rounded p-0.5 text-muted hover:text-danger" title="移除">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 共享规格 */}
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
          <div>
            <label className={labelCls}>每户初始资金 (元)</label>
            <input type="number" value={cash} onChange={e => setCash(e.target.value)} className={cn(inputCls, 'mt-1')} placeholder="200000" />
          </div>
          <div>
            <label className={labelCls}>账户名前缀 (可选)</label>
            <input value={prefix} onChange={e => setPrefix(e.target.value)} className={cn(inputCls, 'mt-1 font-sans')} placeholder="如: 第一期-" />
          </div>
          <div>
            <label className={labelCls}>交易市场</label>
            <select value={market} onChange={e => {
          const next = e.target.value as Market
          setMarket(next)
          setStampQian(defaultStampQian(next, defaults.stampQian))
        }} className={cn(inputCls, 'mt-1 font-sans')}>
              <option value="cn">A股</option>
              <option value="hk">港股</option>
              <option value="us">美股</option>
            </select>
          </div>
          {rows.length > 0 && rows.every(r => r.kind === 'strategy') ? (
            <div className="self-end pb-1.5 text-[10px] leading-4 text-muted">
              入场信号买入 · 出场信号全平
            </div>
          ) : (
            <div>
              <label className={labelCls}>方向 (监控规则来源)</label>
              <div className="mt-1 flex gap-1.5">
                {(['buy', 'sell'] as const).map(sd => (
                  <button key={sd} onClick={() => setSide(sd)}
                    className={cn('flex-1 whitespace-nowrap rounded-btn border py-1 text-[11px] font-medium transition-colors',
                      side === sd ? (sd === 'buy' ? 'border-bull/50 bg-bull/10 text-bull' : 'border-bear/50 bg-bear/10 text-bear') : 'border-border text-muted')}>
                    {sd === 'buy' ? '买入' : '卖出'}
                  </button>
                ))}
              </div>
            </div>
          )}
          <div>
            <label className={labelCls}>仓位模式 (全部来源)</label>
            <div className="mt-1 flex gap-1.5">
              <button onClick={() => setSizeMode('fixed_amount')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'fixed_amount' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>固定金额</button>
              <button onClick={() => setSizeMode('pct_equity')}
                className={cn('flex-1 rounded-btn border py-1 text-[11px] transition-colors', sizeMode === 'pct_equity' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted')}>权益 %</button>
            </div>
          </div>
          <div>
            <label className={labelCls}>{sizeMode === 'fixed_amount' ? '每笔金额 (元)' : '每笔占权益 %'}</label>
            <input type="number" value={sizeValue} onChange={e => setSizeValue(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label className={labelCls}>订单类型</label>
            <div className="mt-1 flex gap-1">
              {(['next_open', 'close', 'market'] as const).map(t => (
                <button key={t} onClick={() => setOrderType(t)}
                  className={cn('flex-1 rounded-btn border py-1 text-[10px] transition-colors',
                    orderType === t ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary')}>
                  {ORDER_TYPE_LABEL[t]}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className={labelCls}>冷却天数 (同标的)</label>
            <input type="number" value={cooldown} onChange={e => setCooldown(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div>
            <label className={labelCls} title="最多同时持有的标的数 (买入开新仓受限, 0 = 不限) — 与回测 max_positions 同口径">最大持仓 (标的数)</label>
            <input type="number" min="0" step="1" value={maxPositions} onChange={e => setMaxPositions(e.target.value)} className={cn(inputCls, 'mt-1')} />
          </div>
          <div className="grid grid-cols-3 gap-2 md:col-span-2">
            <div>
              <label className={labelCls}>佣金 (万)</label>
              <input type="number" step="0.1" min="0" value={commissionWan} onChange={e => setCommissionWan(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>印花税 (千)</label>
              <input type="number" step="0.1" min="0" value={stampQian} onChange={e => setStampQian(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
            <div>
              <label className={labelCls}>滑点 (bps)</label>
              <input type="number" step="1" min="0" value={slippageBps} onChange={e => setSlippageBps(e.target.value)} className={cn(inputCls, 'mt-1')} />
            </div>
          </div>
        </div>

        <div className="mt-4 flex items-center gap-2">
          <button
            onClick={() => valid && m.mutate()}
            disabled={!valid || m.isPending}
            className="flex-1 rounded-btn bg-accent py-2 text-sm font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-40"
          >
            {m.isPending ? '创建中…' : `创建 ${rows.length || ''} 个账户`.replace('  ', ' ')}
          </button>
          <button onClick={onClose} className="rounded-btn border border-border px-4 py-2 text-sm text-secondary transition-colors hover:bg-elevated hover:text-foreground">取消</button>
        </div>
        {m.isError && <div className="mt-2 rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{String((m.error as Error).message)}</div>}
        <div className="mt-2 text-[10px] leading-relaxed text-muted">
          创建后各账户立即参与盘中自动跟单 (规则触发 → 下单 → {market === 'cn' ? 'T+1' : 'T+0'} 约束), 每交易日盘后管道统一结算定版净值, 对比榜单自动更新。
        </div>
      </div>
    </Modal>
  )
}

/** 日期加减 (UTC, 只做 K 线窗口前后留白, 不参与交易日期判断) */
function shiftIsoDate(day: string, delta: number): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + delta)
  return d.toISOString().slice(0, 10)
}

/** 单标的成交回放: 日 K + 买卖标记 + 成交明细 + 区间统计。只读，不修改台账。 */
function PaperReplayModal({ acc, symbol, onClose }: { acc: string; symbol: string; onClose: () => void }) {
  const [chartWindow, setChartWindow] = useState<'all' | '1y'>('all')
  const replayQ = useQuery({
    queryKey: QK.paperReplay(acc, symbol),
    queryFn: () => api.paperReplay(symbol, { account: acc }),
  })
  const data: PaperReplayResponse | undefined = replayQ.data

  const chartRange = useMemo(() => {
    const base = getDefaultRange()
    if (chartWindow === '1y' || !data) return base
    const dates = data.fills.map(f => f.date).filter(Boolean)
    if (dates.length === 0) return base
    const first = dates.reduce((a, b) => (a < b ? a : b))
    const last = dates.reduce((a, b) => (a > b ? a : b))
    return {
      start: first < base.start ? shiftIsoDate(first, -20) : base.start,
      end: last > base.end ? shiftIsoDate(last, 20) : base.end,
    }
  }, [data, chartWindow])

  const markers: ChartMarker[] = useMemo(
    () => (data?.markers ?? []).map(m => ({
      date: m.date,
      kind: m.kind,
      label: m.label,
      above: m.above,
      ...(m.kind === 'neutral' ? { color: '#8B5CF6' } : {}),
    })),
    [data],
  )

  const s = data?.summary
  const winRate = s?.win_rate
  return (
    <Modal
      onClose={onClose}
      labelledBy="paper-replay-title"
      panelClassName="w-[96vw] max-w-6xl max-h-[92vh] overflow-y-auto bg-surface border border-border rounded-card shadow-xl"
    >
      <div className="p-5">
        <div className="flex flex-wrap items-center gap-2">
          <TrendingUp className="h-4 w-4 text-accent" />
          <h3 id="paper-replay-title" className="text-sm font-semibold text-foreground">
            成交回放
            <span className="ml-2 font-mono text-[11px] text-muted">{data?.symbol ?? symbol}</span>
            {data?.name && data.name !== data.symbol && (
              <span className="ml-1.5 text-[11px] font-normal text-secondary">{data.name}</span>
            )}
          </h3>
          {data && (
            <span className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-muted">
              {data.market.toUpperCase()}
            </span>
          )}
          {data?.strategy?.name && (
            <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] text-accent" title={data.strategy.source ?? undefined}>
              策略 · {data.strategy.name}
            </span>
          )}
          <button
            onClick={onClose}
            className="ml-auto rounded-btn border border-border px-2.5 py-1 text-xs text-muted transition-colors hover:border-accent/40 hover:text-accent"
          >
            关闭
          </button>
        </div>

        {replayQ.isLoading && <div className="py-12 text-center text-xs text-muted">加载回放数据…</div>}
        {replayQ.isError && (
          <div className="mt-4 rounded-btn bg-danger/10 px-3 py-2 text-xs text-danger">
            回放加载失败: {String((replayQ.error as Error).message)}
          </div>
        )}

        {data && s && (
          <>
            <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <StatCard label="买入" value={`${s.buys} 笔`} sub={fmtMoney(s.buy_amount, 0)} />
              <StatCard label="卖出" value={`${s.sells} 笔`} sub={fmtMoney(s.sell_amount, 0)} />
              <StatCard label="费用" value={fmtMoney(s.fees)} />
              <StatCard
                label="已实现盈亏"
                value={`${s.realized_pnl >= 0 ? '+' : ''}${fmtMoney(s.realized_pnl, 0)}`}
                valueClass={priceColorClass(s.realized_pnl)}
              />
              <StatCard label="胜率" value={winRate == null ? '--' : `${winRate.toFixed(1)}%`} sub={`${s.rounds} 个回合`} />
              <StatCard label="期末持仓" value={`${s.open_qty} 股`} sub={s.avg_holding_days == null ? undefined : `均持 ${s.avg_holding_days} 天`} />
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <span>图表窗口</span>
              {(['all', '1y'] as const).map(w => (
                <button
                  key={w}
                  onClick={() => setChartWindow(w)}
                  className={cn(
                    'rounded-btn border px-2 py-0.5 transition-colors',
                    chartWindow === w ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border text-muted hover:text-secondary',
                  )}
                >
                  {w === 'all' ? '全部成交' : '近一年'}
                </button>
              ))}
              {data.truncated && (
                <span className="text-warning">成交过多，明细仅显示最近 {data.fills.length} / {data.total_fills} 笔</span>
              )}
              {s.corp_actions > 0 && <span>区间内 {s.corp_actions} 次除权调整</span>}
            </div>

            <div className="mt-2 rounded-card border border-border bg-base/30 p-2">
              <StockDailyKChart
                symbol={data.symbol}
                dateRange={chartRange}
                markers={markers}
                height={360}
                visibleBars={120}
                showLimitMarkers={false}
                showIndicatorControls={false}
                showMarkerToggle={false}
                showMA
              />
            </div>

            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[640px] text-xs">
                <thead>
                  <tr className="text-left text-[10px] text-muted">
                    <th className="py-1.5 font-normal">日期</th>
                    <th className="py-1.5 font-normal">方向</th>
                    <th className="py-1.5 text-right font-normal">数量</th>
                    <th className="py-1.5 text-right font-normal">成交价</th>
                    <th className="py-1.5 text-right font-normal">费用</th>
                    <th className="py-1.5 font-normal">原因</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {data.fills.length === 0 && (
                    <tr><td colSpan={6} className="py-8 text-center font-sans text-xs text-muted">区间内无成交</td></tr>
                  )}
                  {[...data.fills].reverse().map(f => (
                    <tr key={`${f.seq}-${f.order_id ?? f.date}`} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                      <td className="py-1.5 text-muted">{f.date}</td>
                      <td className={cn('py-1.5 font-sans font-medium', f.side === 'buy' ? 'text-bull' : 'text-bear')}>
                        {f.side === 'buy' ? '买入' : '卖出'}
                      </td>
                      <td className="py-1.5 text-right">{f.qty}</td>
                      <td className="py-1.5 text-right">{fmtMoney(f.price, 3)}</td>
                      <td className="py-1.5 text-right text-muted">{fmtMoney(f.fee)}</td>
                      <td className="py-1.5 font-sans text-secondary">
                        {f.reason_label}
                        {f.reason && f.reason !== f.reason_label && (
                          <span className="ml-1 text-[10px] text-muted">{f.reason}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}

export function Paper() {
  const qc = useQueryClient()
  // 新建账户草稿 id: 打开弹窗时生成, 关闭即丢弃, 不写任何持久状态。
  const [draftId, setDraftId] = useState<string | null>(null)
  const [setupOpen, setSetupOpen] = useState(false)
  const close = () => { setSetupOpen(false); setDraftId(null) }
  return (
    <>
      <CompareView onCreateSingle={() => { setDraftId(genAccountId()); setSetupOpen(true) }} />
      {setupOpen && (
        <Modal onClose={close} labelledBy="setup-card-title" panelClassName="w-[94vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
          <SetupCard
            accId={draftId ?? 'default'}
            onDone={() => {
              qc.invalidateQueries({ queryKey: QK.paperAll })
              close()
            }}
            onCancel={close}
          />
        </Modal>
      )}
    </>
  )
}

/** 北京时间交易日 (与行情/撮合口径一致), 用于「本日委托/成交」计数。 */
function paperTradingDay(): string {
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  } catch {
    return new Date().toISOString().slice(0, 10)
  }
}

const ENGINE_STATE_LABEL: Record<string, string> = {
  running: '运行中',
  waiting: '等待信号',
  paused: '已暂停',
  blocked: '已阻断',
  idle: '待接入',
}

const LIVE_STATUS_LABEL: Record<string, string> = {
  pending: '撮合中',
  filled: '已成交',
  cancelled: '已撤单',
  expired: '已过期',
}

const fmtFeedTime = (s?: string | null) => (s && s.length >= 19 ? s.slice(11, 19) : '—')

/** 指挥台时钟: 每秒走字, 给页面一个持续的心跳。 */
function DeskClock() {
  const [clock, setClock] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setClock(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return (
    <span className="tf-desk-clock font-mono text-[10px] text-muted">
      {clock.toLocaleTimeString('zh-CN', { hour12: false })} CST
    </span>
  )
}

interface FlowSymbol {
  symbol: string
  name: string
  price: number | null
  changePct: number | null
  side: 'buy' | 'sell'
  count: number
}

interface FlowStrategy {
  id: string
  name: string
  account: string
  accountName: string
  enabled: boolean
  kind: string
  side: 'buy' | 'sell'
}

/** 资金全景: 行情 → 策略 → 账户 三栏资本流, 曲线与粒子持续流动。
 *  右栏账户每 5s 自动轮播 (悬停暂停), 点击策略/节点可立即跳转。 */
function CapitalFlowStage({ symbols, strategies, accounts, focusAccount, orders, totals, onOpenTerminal, onSelectAccount }: {
  symbols: FlowSymbol[]
  strategies: FlowStrategy[]
  accounts: PaperCompareRow[]
  focusAccount: PaperCompareRow | null
  orders: PaperLiveItem[]
  totals?: PaperLiveTotals
  onOpenTerminal: (acc: PaperCompareRow) => void
  onSelectAccount: (acc: string) => void
}) {
  const [idx, setIdx] = useState(0)
  // 轮播顺序: 最近有真实委托的账户优先; 顺序仅在账户集合变化时重排,
  // 避免每 5s 流水刷新导致下标跳号 (表现为「暂停/卡顿」)。
  const rotation = useMemo(() => {
    const withOrders = new Set(orders.map(o => o.account))
    return [
      ...accounts.filter(a => withOrders.has(a.account)),
      ...accounts.filter(a => !withOrders.has(a.account)),
    ]
  }, [accounts.map(a => a.account).join('|'), orders.length > 0])
  const count = rotation.length
  // rotation 会随数据刷新换引用, 用 ref 避免把自动切换的下标重置回去
  const rotationRef = useRef(rotation)
  rotationRef.current = rotation

  // 外部点击节点/策略时跳到该账户
  useEffect(() => {
    if (!focusAccount) return
    const i = rotationRef.current.findIndex(a => a.account === focusAccount.account)
    if (i >= 0) setIdx(i)
  }, [focusAccount?.account])

  // 每 5s 轮播下一个账户 (不因悬停/数据刷新暂停)
  useEffect(() => {
    if (count <= 1) return
    const id = window.setInterval(() => setIdx(i => (i + 1) % count), 5000)
    return () => window.clearInterval(id)
  }, [count])

  const account = rotation[Math.min(idx, Math.max(0, count - 1))] ?? focusAccount ?? null
  const accountOrders = account ? orders.filter(o => o.account === account.account) : []

  // 光子通道: 左栏标的 → 策略核心 → 右栏账户。走三栏之间的空档, 不做满屏连线。
  const slotCoreX = 500
  const flows = useMemo(() => {
    const out: Array<{ id: string; d: string; side: 'buy' | 'sell'; dur: number; bus?: boolean }> = []
    symbols.slice(0, 7).forEach((s, i) => {
      const y = 92 + i * 58
      out.push({
        id: `in-${s.symbol}-${i}`,
        d: `M 206 ${y} C 320 ${y} 360 330 ${slotCoreX} 330`,
        side: s.side,
        dur: 3.4 + (i % 3) * 0.55,
      })
    })
    strategies.slice(0, 5).forEach((s, i) => {
      const y = 120 + i * 92
      out.push({
        id: `out-${s.account}-${i}`,
        d: `M ${slotCoreX} 330 C 640 330 664 ${y} 792 ${y}`,
        side: s.side,
        dur: 3.8 + (i % 3) * 0.6,
      })
    })
    // 通道内的直线光子带: 只在两条 100px 空档里水平跳跃, 方向最直观
    ;[0, 1, 2].forEach(r => {
      const y = 152 + r * 158
      out.push({
        id: `bus-a-${r}`,
        d: `M 240 ${y} L 322 ${y}`,
        side: r % 2 === 0 ? 'buy' : 'sell',
        dur: 1.6,
        bus: true,
      })
      out.push({
        id: `bus-b-${r}`,
        d: `M 678 ${y} L 760 ${y}`,
        side: r % 2 === 0 ? 'buy' : 'sell',
        dur: 1.6,
        bus: true,
      })
    })
    return out
  }, [symbols, strategies])

  const pct = account
    ? (account.estimating ? (account.live_pnl_pct ?? account.pnl_pct ?? 0) : (account.pnl_pct ?? 0))
    : 0
  const total = account
    ? (account.estimating ? (account.live_total ?? account.total) : account.total)
    : 0
  // 纵向无缝滚动: 少量标的时复制一份
  const symbolRail = symbols.length > 4 ? [...symbols, ...symbols] : symbols
  const activeAccounts = totals?.active_accounts ?? new Set(orders.map(o => o.account)).size
  const buyCount = totals?.buys ?? orders.filter(o => o.side === 'buy').length
  const sellCount = totals?.sells ?? orders.length - orders.filter(o => o.side === 'buy').length
  const orderTotal = totals?.orders ?? orders.length
  const latestNav = account?.nav?.at(-1)?.nav ?? null
  // 现金/市值构成条 (真实账户口径)
  const cashPct = account && total > 0 ? Math.max(0, Math.min(100, (account.cash / total) * 100)) : 0
  const tapeItems = orders.slice(0, 14)

  return (
    <section className="tf-capital" aria-label="资金全景">
      <span className="tf-capital-grid" aria-hidden="true" />
      <header className="tf-capital-head">
        <span className="tf-live-dot" aria-hidden="true" />
        <span className="font-mono text-[10px] tracking-[0.24em] text-muted">CAPITAL PANORAMA</span>
        <span className="tf-cockpit-sub">行情 → 策略 → 账户 · 实时资本流向</span>
        <span className="tf-cockpit-live ml-auto">
          {accounts.length} ACCOUNTS · {symbols.length} SYMBOLS · {orders.length} FILLS · {strategies.filter(s => s.enabled).length}/{strategies.length} STRATEGIES
        </span>
      </header>

      <div className="tf-capital-stage">
        <svg className="tf-capital-flow" viewBox="0 0 1000 620" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker
              id="tf-flow-arrow"
              viewBox="0 0 6 6"
              refX="5"
              refY="3"
              markerWidth="6"
              markerHeight="6"
              orient="auto"
            >
              <path d="M0,0 L6,3 L0,6 Z" />
            </marker>
            <marker
              id="tf-flow-arrow-lg"
              viewBox="0 0 6 6"
              refX="5"
              refY="3"
              markerWidth="13"
              markerHeight="13"
              orient="auto"
            >
              <path d="M0,0 L6,3 L0,6 Z" />
            </marker>
          </defs>
          {flows.map((f, i) => (
            <g key={f.id}>
              <path
                d={f.d}
                className={cn('tf-flow-lane', f.bus && 'is-bus')}
                markerEnd={f.bus ? 'url(#tf-flow-arrow-lg)' : 'url(#tf-flow-arrow)'}
              />
              {(f.bus ? [0, 1] : [0, 1, 2, 3]).map(k => (
                <g key={k} className={cn('tf-photon', f.side === 'buy' ? 'is-buy' : 'is-sell', f.bus && 'is-bus')}>
                  <animateMotion
                    dur={`${f.dur}s`}
                    repeatCount="indefinite"
                    path={f.d}
                    begin={`${((k * f.dur) / 4 + (i % 5) * 0.23).toFixed(2)}s`}
                    calcMode="discrete"
                    keyPoints="0;0.14;0.28;0.42;0.56;0.7;0.84;1"
                    keyTimes="0;0.13;0.26;0.39;0.52;0.65;0.78;0.91"
                  />
                  <circle r={f.bus ? 9 : 7.5} className="tf-photon-halo" />
                  <circle r={f.bus ? 3.2 : 2.6} className="tf-photon-core" />
                </g>
              ))}
            </g>
          ))}
        </svg>

        <div className="tf-capital-col is-left">
          <div className="tf-col-head">
            <span>MARKET UNIVERSE</span>
            <b>{symbols.length}</b>
          </div>
          <div className="tf-symbol-sub">
            <span>标的</span>
            <span>现价</span>
            <span>涨跌</span>
          </div>
          {symbols.length === 0 ? (
            <div className="tf-col-empty">
              <span className="tf-live-wave" aria-hidden="true"><i /><i /><i /><i /><i /></span>
              <span>等待成交回报…</span>
            </div>
          ) : (
            <div className="tf-symbol-rail">
              <div className="tf-symbol-track">
                {symbolRail.map((s, i) => (
                  <div
                    key={`${s.symbol}-${i}`}
                    className={cn('tf-symbol-row', s.side === 'buy' ? 'is-buy' : 'is-sell')}
                    title={`${s.name || s.symbol} · ${s.symbol} · ${s.count} 笔`}
                  >
                    <span className="tf-symbol-main">
                      <span className="tf-symbol-name">{s.name || s.symbol}</span>
                      <span className="tf-symbol-code">{s.symbol}{s.count > 1 ? ` · ${s.count}笔` : ''}</span>
                    </span>
                    <span className="tf-symbol-quote">
                      <b className="tf-symbol-price">{s.price != null ? fmtMoney(s.price, 2) : '—'}</b>
                      <span className={cn('tf-symbol-pct', s.changePct != null ? priceColorClass(s.changePct / 100) : 'text-muted')}>
                        {s.changePct != null ? fmtPct(s.changePct / 100) : '—'}
                      </span>
                    </span>
                    <span className="tf-symbol-side">{s.side === 'buy' ? 'B' : 'S'}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="tf-signal-gutter is-a" aria-hidden="true">
          <span className="tf-gutter-tag">SIG</span>
          {[0, 1, 2, 3, 4, 5].map(r => (
            <span key={r} className="tf-gutter-chev">
              <i>›</i><i>›</i><i>›</i>
            </span>
          ))}
        </div>

        <div className="tf-capital-col is-core">
          <span className="tf-core-halo" aria-hidden="true" />
          <div className="tf-core-head">
            <span className="tf-core-orb" aria-hidden="true" />
            <div className="min-w-0">
              <div className="tf-col-head"><span>STRATEGY LIBRARY</span></div>
              <div className="text-[9px] text-muted">
                {strategies.length > 0
                  ? `${strategies.filter(s => s.enabled).length} 启用 / ${strategies.length} 策略`
                  : '策略库同步中'}
              </div>
            </div>
          </div>
          <div className="tf-strategy-grid">
            {strategies.map(s => (
              <button
                key={s.id}
                type="button"
                onClick={() => onSelectAccount(s.account)}
                title={`${s.name} · ${s.accountName} · ${s.enabled ? '已启用' : '已停用'}`}
                className={cn('tf-strategy-item', !s.enabled && 'is-off', s.account === account?.account && 'is-active')}
              >
                <span className={cn('tf-strategy-led', s.side === 'buy' ? 'is-buy' : 'is-sell')} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1">
                    <span className="truncate text-[12px]">{s.name}</span>
                    <span className={cn('tf-strategy-tag', !s.enabled && 'is-off')}>
                      {s.kind === 'strategy' ? 'STRAT' : 'RULE'}
                    </span>
                  </span>
                  <span className="block truncate font-mono text-[9px] text-muted">{s.accountName}</span>
                </span>
                <span className={cn('font-mono text-[9px]', s.enabled ? 'text-accent' : 'text-muted')}>
                  {s.enabled ? 'ON' : 'OFF'}
                </span>
              </button>
            ))}
            {strategies.length === 0 && <div className="tf-col-empty">策略库同步中…</div>}
          </div>
          <div className="tf-core-metrics">
            <div><span>委托流水</span><b>{orderTotal}</b></div>
            <div><span>买 / 卖</span><b>{buyCount}/{sellCount}</b></div>
            <div><span>活跃账户</span><b>{activeAccounts}</b></div>
          </div>
        </div>

        <div className="tf-signal-gutter is-b" aria-hidden="true">
          <span className="tf-gutter-tag">OUT</span>
          {[0, 1, 2, 3, 4, 5].map(r => (
            <span key={r} className="tf-gutter-chev is-tall">
              <i>›</i><i>›</i><i>›</i>
            </span>
          ))}
        </div>

        <div className="tf-capital-col is-right">
          {account ? (
            <div key={account.account} className="tf-account-card">
              <span className="tf-account-watermark" aria-hidden="true">{String(idx + 1).padStart(2, '0')}</span>
              <span
                key={`p-${account.account}-${idx}`}
                className="tf-account-progress"
                aria-hidden="true"
              />
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="tf-account-name truncate">{account.name}</span>
                    <span className="tf-account-idx">{idx + 1}/{count}</span>
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[9px] text-muted">
                    <span className="truncate">{account.account}</span>
                    <span className="tf-account-badge">AUTO 5s</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => onOpenTerminal(account)}
                  className="rounded-btn border border-accent/40 px-2 py-0.5 text-[10px] text-accent transition-colors hover:bg-accent/10"
                >
                  执行终端 ↗
                </button>
              </div>
              <div className="mt-3">
                <div className="text-[9px] tracking-[0.16em] text-muted">TOTAL ASSET</div>
                <div className="flex items-baseline gap-2">
                  <AnimatedNumber
                    value={total}
                    format={v => fmtMoney(v, 0)}
                    resetKey={account.last_nav_date ?? account.account}
                    className="font-mono text-[34px] font-semibold leading-none tabular tracking-tight"
                  />
                  <span className={cn('font-mono text-sm tabular', priceColorClass(pct / 100))}>
                    {fmtPct(pct / 100)}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center gap-3 font-mono text-[9px] text-muted">
                  <span>
                    今日{' '}
                    <b className={cn('tabular', account.day_change_pct != null ? priceColorClass(account.day_change_pct / 100) : '')}>
                      {account.day_change_pct != null ? fmtPct(account.day_change_pct / 100) : '—'}
                    </b>
                  </span>
                  <span>净值 <b className="tabular text-secondary">{latestNav != null ? fmtMoney(latestNav, 3) : '—'}</b></span>
                  <span>初始 <b className="tabular text-secondary">{fmtMoney(account.initial_cash, 0)}</b></span>
                </div>
                <div className="tf-account-compose">
                  <div className="tf-compose-bar">
                    <i style={{ width: `${cashPct}%` }} />
                    <b style={{ width: `${100 - cashPct}%` }} />
                  </div>
                  <div className="tf-compose-legend">
                    <span>现金 {fmtMoney(account.cash, 0)}</span>
                    <span>市值 {fmtMoney(account.market_value, 0)}</span>
                  </div>
                </div>
              </div>
              <div className="tf-account-stats">
                {[
                  ['持仓', String(account.holdings_count ?? 0)],
                  ['回合', String(account.rounds)],
                  ['胜率', `${account.win_rate.toFixed(1)}%`],
                  ['回撤', account.max_drawdown != null ? `-${account.max_drawdown.toFixed(1)}%` : '—'],
                  ['已实现', account.realized_pnl != null ? fmtMoney(account.realized_pnl, 0) : '—'],
                  ['盈亏比', account.profit_loss_ratio != null ? account.profit_loss_ratio.toFixed(2) : '—'],
                  ['市值', fmtMoney(account.market_value, 0)],
                  ['费率', `${(account.fees.commission_pct * 10000).toFixed(1)}bp`],
                ].map(([k, v]) => (
                  <div key={k}>
                    <div className="tf-stat-k">{k}</div>
                    <div className="tf-stat-v">{v}</div>
                  </div>
                ))}
              </div>
              <div className="tf-col-head mt-2.5"><span>RECENT ORDERS</span><b>{accountOrders.length}</b></div>
              <div className={cn('tf-account-orders', accountOrders.length === 0 && 'is-empty')}>
                {accountOrders.length === 0 && (
                  <div className="tf-orders-idle">
                    <span className="tf-live-wave" aria-hidden="true"><i /><i /><i /><i /><i /></span>
                    <span>该账户暂无成交 · 轮播继续</span>
                  </div>
                )}
                {accountOrders.slice(0, 8).map(o => (
                  <div
                    key={o.id}
                    className={cn('tf-order-row', o.side === 'buy' ? 'is-buy' : 'is-sell')}
                  >
                    <span className="w-11 shrink-0 font-mono text-[9px] text-muted">{fmtFeedTime(o.created_at)}</span>
                    <span className="w-16 shrink-0 font-mono text-[9px]">{o.symbol}</span>
                    <b className={cn('w-7 shrink-0 font-mono text-[9px]', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
                      {o.side === 'buy' ? 'BUY' : 'SELL'}
                    </b>
                    <span className="font-mono text-[9px] text-muted">×{o.qty}</span>
                    <span className="ml-auto font-mono text-[9px] text-secondary">
                      {o.fill_price != null ? fmtMoney(o.fill_price, 2) : '—'}
                    </span>
                    <span className={cn('tf-feed-state shrink-0', `is-${o.status}`)}>
                      {LIVE_STATUS_LABEL[o.status] ?? o.status}
                    </span>
                  </div>
                ))}
              </div>
              <div className="tf-account-pager">
                {rotation.map((a, i) => (
                  <button
                    key={a.account}
                    type="button"
                    onClick={() => setIdx(i)}
                    aria-label={a.name}
                    className={cn('tf-pager-dot', i === idx && 'is-active')}
                  />
                ))}
                <span className="ml-auto text-[9px] text-muted">5s 轮播</span>
              </div>
            </div>
          ) : (
            <div className="tf-col-empty">暂无模拟账户</div>
          )}
        </div>
      </div>

      {tapeItems.length > 0 && (
        <div className="tf-capital-tape" aria-hidden="true">
          <span className="tf-tape-tag">LIVE FILLS</span>
          <div className="tf-tape-rail">
            <div className="tf-tape-track">
              {[...tapeItems, ...tapeItems].map((o, i) => (
                <span key={`${o.id}-${i}`} className={cn('tf-tape-item', o.side === 'buy' ? 'is-buy' : 'is-sell')}>
                  <b>{o.side === 'buy' ? 'BUY' : 'SELL'}</b>
                  <span className="tf-tape-sym">{o.symbol}</span>
                  <span className="tf-tape-qty">×{o.qty}</span>
                  <span className="tf-tape-px">@{o.fill_price != null ? fmtMoney(o.fill_price, 2) : '—'}</span>
                  <span className="tf-tape-acc">{o.name}</span>
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

/** 策略引擎控制台: 引擎状态 + 管道阶段 + 实时订单流。
 *  全部数据来自真实跟单状态/订单/成交, 无数据时退化为雷达待机态。 */
function PaperLiveConsole({ account, ov, orders, fills, symbolNames }: {
  account: string
  ov: {
    total?: number
    total_pnl?: number
    initial_cash?: number
    price_date?: string | null
    holdings?: PaperHolding[]
  }
  orders: PaperOrder[]
  fills: PaperFill[]
  symbolNames: Record<string, string>
}) {
  const bindingQ = useQuery({
    queryKey: executionKey(account),
    queryFn: () => executionApi.state(account),
    refetchInterval: 15_000,
    retry: false,
  })
  const binding: ExecutionBinding | null = bindingQ.data?.binding ?? null
  // 逐笔行情通道: 订阅当前持仓, 每拍闪烁最新价 (休市时退回账户快照价)
  const holdings = (ov.holdings ?? []).slice(0, 6)
  const ticks = useSymbolTicks(holdings.map(h => h.symbol), holdings.length > 0)
  const tickCount = Object.keys(ticks).length
  const day = ov.price_date || paperTradingDay()
  const todays = orders.filter(o => (o.created_at ?? '').startsWith(day))
  const todayFills = fills.filter(f => f.date === day && f.kind !== 'corp_action')
  const pending = orders.filter(o => o.status === 'pending')
  const recent = orders.slice(0, 6)
  const lastFill = todayFills[todayFills.length - 1]

  const state = !binding
    ? 'idle'
    : binding.enabled
      ? binding.status === 'blocked' ? 'blocked' : 'running'
      : 'paused'
  const live = state === 'running'
  const stages = [
    { key: 'signal', label: '信号', active: state === 'running', value: binding?.last_bar || '—' },
    { key: 'risk', label: '风控', active: state === 'running' && !binding?.risk_message, value: binding?.risk_message ? '拦截' : '通过' },
    { key: 'order', label: '委托', active: pending.length > 0, value: String(pending.length) },
    { key: 'fill', label: '成交', active: todayFills.length > 0, value: String(todayFills.length) },
  ]

  return (
    <section className="tf-panel tf-engine-console rounded-card border border-border bg-surface p-4" aria-label="策略引擎实时执行">
      <span className="tf-engine-scan" aria-hidden="true" />
      <div className="relative flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn('tf-live-dot', !live && 'is-idle')} aria-hidden="true" />
            <span className="font-mono text-[10px] tracking-[0.22em] text-muted">STRATEGY ENGINE</span>
            <span className={cn('tf-engine-state', `is-${state}`)}>{ENGINE_STATE_LABEL[state]}</span>
            <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px] text-muted">
              <Cpu className="h-3 w-3" />
              交易日 {day}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="truncate text-sm font-medium">
              {binding ? binding.snapshot.name : '未接入策略快照'}
            </span>
            {binding?.last_bar && (
              <span className="font-mono text-[10px] text-muted">最近信号日 {binding.last_bar}</span>
            )}
            {bindingQ.isError && <span className="text-[10px] text-warning">跟单状态读取失败</span>}
          </div>
          <PulseLine live={live} className="mt-2 h-5 w-full max-w-[220px]" />
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            {stages.map((s, i) => (
              <Fragment key={s.key}>
                {i > 0 && <span className="tf-engine-arrow" aria-hidden="true">›</span>}
                <span
                  className={cn('tf-engine-stage', s.active && 'is-active')}
                  title={`${s.label}: ${s.value}`}
                >
                  <span className="tf-engine-stage-label">{s.label}</span>
                  <b className="font-mono">{s.value}</b>
                </span>
              </Fragment>
            ))}
          </div>
        </div>
        <div className="grid w-full grid-cols-3 gap-2 sm:w-auto sm:min-w-[268px]">
          {[
            { label: '本日委托', value: todays.length, cls: '' },
            { label: '待成交', value: pending.length, cls: pending.length > 0 ? 'text-accent' : '' },
            { label: '本日成交', value: todayFills.length, cls: todayFills.length > 0 ? 'text-bull' : '' },
          ].map(m => (
            <div key={m.label} className="tf-engine-metric rounded-btn border border-border/70 px-2.5 py-1.5">
              <div className="text-[9px] tracking-[0.14em] text-muted">{m.label}</div>
              <AnimatedNumber
                value={m.value}
                resetKey={day}
                className={cn('font-mono text-lg font-semibold tabular', m.cls)}
              />
            </div>
          ))}
        </div>
      </div>

      <div className="relative mt-3 border-t border-border/60 pt-2.5">
        {holdings.length > 0 && (
          <div className="tf-tick-strip mb-2.5">
            <span className="inline-flex shrink-0 items-center gap-1.5 font-mono text-[9px] tracking-[0.16em] text-muted">
              {tickCount > 0 && <span className="tf-live-dot" aria-hidden="true" />}
              TICKS
            </span>
            {holdings.map(h => {
              const t = ticks[h.symbol]
              const price = t?.last_price ?? h.last_price
              const pct = t?.change_pct
              return (
                <span key={h.symbol} className="tf-tick-cell">
                  <span className="font-mono text-[10px] text-muted">{h.symbol}</span>
                  <span
                    key={`${h.symbol}-${t?.ts ?? 0}`}
                    className={cn(
                      'tf-tick-price',
                      t?.dir === 'up' && 'is-up',
                      t?.dir === 'down' && 'is-down',
                    )}
                  >
                    {fmtMoney(price, 2)}
                  </span>
                  <span className={cn('font-mono text-[10px] tabular', pct != null ? priceColorClass(pct / 100) : 'text-muted')}>
                    {pct != null ? fmtPct(pct / 100) : '—'}
                  </span>
                </span>
              )
            })}
            <span className="ml-auto shrink-0 text-[9px] text-muted">
              {tickCount > 0 ? '逐笔推送' : '快照'}
            </span>
          </div>
        )}
        <div className="flex items-center gap-2">
          <Activity className="h-3.5 w-3.5 text-accent" />
          <span className="text-xs font-medium">实时订单流</span>
          <span className="font-mono text-[10px] text-muted">{orders.length} 笔</span>
          {pending.length > 0 && (
            <span className="tf-engine-live-tag">撮合中 {pending.length}</span>
          )}
          {lastFill?.symbol && (
            <span key={lastFill.seq} className="tf-engine-tick ml-auto font-mono text-[10px]">
              最近成交 {lastFill.symbol}
              <span className={lastFill.side === 'buy' ? 'text-bull' : 'text-bear'}>
                {lastFill.side === 'buy' ? ' 买入' : ' 卖出'}
              </span>
              {lastFill.price != null ? ` @ ${fmtMoney(lastFill.price, 3)}` : ''}
            </span>
          )}
        </div>
        {recent.length === 0 ? (
          <div className="tf-engine-idle">
            <span className="tf-radar" aria-hidden="true" />
            <div>
              <div className="text-[11px] text-secondary">等待策略信号</div>
              <div className="text-[10px] text-muted">
                {binding ? '已接入策略, 买卖点触发后自动在此流转' : '绑定回测参数快照后, 自动跟单委托会在此实时流转'}
              </div>
            </div>
          </div>
        ) : (
          <div className="mt-2 space-y-1">
            {recent.map((o, i) => (
              <div
                key={o.id}
                style={{ animationDelay: `${Math.min(i, 5) * 40}ms` }}
                className={cn('tf-flow-row', o.side === 'buy' ? 'is-buy' : 'is-sell', o.status === 'pending' && 'is-pending')}
              >
                <span className={cn('w-9 shrink-0 font-mono text-[10px] font-semibold', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
                  {o.side === 'buy' ? 'BUY' : 'SELL'}
                </span>
                <span className="w-20 shrink-0 font-mono">{o.symbol}</span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-muted">{symbolNames[o.symbol] ?? ''}</span>
                <span className="shrink-0 font-mono text-muted">{o.qty}</span>
                <span className="w-24 shrink-0 text-right font-mono text-[11px] text-muted">
                  {o.fill_price != null ? `@${fmtMoney(o.fill_price, 3)}` : ORDER_TYPE_LABEL[o.order_type]}
                </span>
                <span className={cn(
                  'w-14 shrink-0 rounded-full py-px text-center text-[10px] leading-4',
                  o.status === 'filled' ? 'bg-bull/10 text-bull'
                    : o.status === 'pending' ? 'bg-accent/10 text-accent'
                    : o.status === 'expired' ? 'bg-warning/10 text-warning'
                    : 'bg-elevated text-muted',
                )}>
                  {STATUS_LABEL[o.status]}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

/** 账户操作面板 (原单账户视图主体): 在对比榜单行展开内渲染, 免跳转。
 *  含总览卡/净值曲线/持仓/订单与成交/回合明细/下单/跟单规则/费用设置/冻结/删除。 */
function AccountPanel({ acc, name, onDeleted }: { acc: string; name?: string; onDeleted?: () => void }) {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'orders' | 'trades' | 'rounds'>('orders')
  const [feeOpen, setFeeOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [replaySymbol, setReplaySymbol] = useState<string | null>(null)
  const [showAllOrders, setShowAllOrders] = useState(false)
  const [showAllTrades, setShowAllTrades] = useState(false)

  const overviewQ = useQuery({ queryKey: QK.paperOverview(acc), queryFn: () => api.paperOverview(acc) })
  const ordersQ = useQuery({ queryKey: QK.paperOrders(acc), queryFn: () => api.paperOrders(undefined, acc) })
  const tradesQ = useQuery({ queryKey: QK.paperTrades(acc), queryFn: () => api.paperTrades(acc) })
  const navQ = useQuery({ queryKey: QK.paperNav(acc), queryFn: () => api.paperNav(acc) })
  const statsQ = useQuery({ queryKey: QK.paperStats(acc), queryFn: () => api.paperStats(acc) })
  const roundsQ = useQuery({ queryKey: QK.paperRounds(acc), queryFn: () => api.paperRounds(acc) })
  const rounds: PaperRound[] = [...(roundsQ.data?.rounds ?? [])].reverse() // 最新回合在前

  // 持仓+回合+订单+成交 代码→名称 (#454): 就地显示名称, 不用切页查询。股票/ETF/指数都覆盖。
  // 回合/台账标的可能已清仓 (不在持仓), 合并去重一起查
  const nameSymbols = Array.from(new Set([
    ...(overviewQ.data?.holdings ?? []).map(h => h.symbol),
    ...rounds.map(r => r.symbol),
    ...(ordersQ.data?.orders ?? []).map(o => o.symbol),
    ...(tradesQ.data?.fills ?? []).map(f => f.symbol),
  ]))
  const namesQ = useQuery({
    queryKey: QK.instrumentNames(nameSymbols),
    queryFn: () => api.instrumentNames(nameSymbols),
    enabled: nameSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = namesQ.data?.names ?? {}

  // 'paper' 前缀兜底失效: 覆盖全部账户的全部查询 (订单变动可能影响净值/统计)
  const invalidateAll = () => qc.invalidateQueries({ queryKey: QK.paperAll })

  const cancelM = useMutation({
    mutationFn: (id: string) => api.paperOrderCancel(id, acc),
    onSuccess: invalidateAll,
  })
  const queueM = useMutation({
    mutationFn: (on: boolean) => api.paperSettings({ queue_limit_orders: on }, acc),
    onSuccess: invalidateAll,
  })

  if (overviewQ.isLoading) {
    return <div className="py-8 text-center text-xs text-muted">加载中…</div>
  }
  // 请求失败与「未开户」分开呈现: 失败给错误态 + 重试, 不伪装成未初始化
  if (overviewQ.isError || !overviewQ.data) {
    return (
      <div className="flex flex-col items-center gap-2 py-8">
        <span className="text-xs text-secondary">账户数据加载失败，请重试</span>
        <button
          type="button"
          onClick={() => overviewQ.refetch()}
          disabled={overviewQ.isFetching}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-accent transition-colors hover:bg-elevated disabled:opacity-50"
        >
          重试
        </button>
      </div>
    )
  }
  const ov = overviewQ.data
  if (!ov?.initialized) {
    return <div className="py-8 text-center text-xs text-muted">账户未初始化</div>
  }

  const holdings: PaperHolding[] = ov.holdings ?? []
  // 全状态订单流水 (含已成交), 默认收最近 20 条, 可展开全部
  const allOrders: PaperOrder[] = ordersQ.data?.orders ?? []
  const orders = showAllOrders ? allOrders : allOrders.slice(0, 20)
  const allFills: PaperFill[] = tradesQ.data?.fills ?? []
  const trades = showAllTrades ? allFills : allFills.slice(0, 30)
  const nav = navQ.data?.nav ?? []
  const stats = statsQ.data
  const pnlPct = ov.initial_cash && ov.initial_cash > 0 ? ((ov.total_pnl ?? 0) / ov.initial_cash) * 100 : 0

  /** 导出全部成交台账 CSV (带 BOM, Excel 可直接打开); 口径与页面「成交台账」一致 */
  const exportTradesCsv = () => {
    if (allFills.length === 0) return
    const esc = (v: string | number | null | undefined) => {
      const s = v == null ? '' : String(v)
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
    }
    const lines = ['日期,代码,方向,数量,成交价,费用,类型,订单号']
    for (const f of allFills) {
      lines.push([
        f.date, f.symbol,
        f.kind === 'corp_action' ? '除权' : f.side === 'buy' ? '买入' : '卖出',
        f.qty ?? '', f.price ?? '', f.fee ?? '',
        f.kind ?? 'fill', f.order_id ?? '',
      ].map(esc).join(','))
    }
    const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `模拟盘成交台账_${acc}_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      {/* 面板头: 账户标识 + 状态徽章 + 费用 + 冻结 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{name || acc}</span>
        <span className="font-mono text-[10px] text-muted">{acc}</span>
        {ov.status === 'frozen' && <span className="rounded-full bg-warning/15 px-2 py-0.5 text-[10px] text-warning">已冻结</span>}
        {ov.queue_limit_orders && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] text-accent" title="触及涨跌停不直接拒单, 转次日开盘重试 (最多顺延 3 日)">
            涨跌停排队
          </span>
        )}
        <button
          onClick={() => setFeeOpen(true)}
          className="ml-auto flex items-center gap-1 rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
          title={`佣金 ${((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ (最低5元) · 印花税 ${((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰ ${ov.market === 'hk' ? '双边' : ov.market === 'us' ? '无印花税' : '仅卖出'} · 滑点 ${ov.fees?.slippage_bps ?? 0}bps — 点击调整`}
        >
          <Settings className="h-3 w-3" />
          佣金 {((ov.fees?.commission_pct ?? 0) * 10000).toFixed(1)}‱ · 印花税 {((ov.fees?.stamp_tax_pct ?? 0) * 1000).toFixed(1)}‰{ov.market === 'hk' ? '双边' : ''} · 滑点 {ov.fees?.slippage_bps ?? 0}bps
        </button>
        <button
          onClick={() => api.paperFreeze(ov.status !== 'frozen', acc).then(invalidateAll)}
          className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-warning/40 hover:text-warning"
        >
          {ov.status === 'frozen' ? '解冻账户' : '冻结账户'}
        </button>
        <button
          onClick={() => setDeleteOpen(true)}
          className="rounded-btn border border-border px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger"
          title="删除账户及其全部数据 (不可恢复)"
        >
          删除账户
        </button>
      </div>
      <div>
        {/* 总览卡片 */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="总资产 (虚拟)" numeric={ov.total} numFormat={v => fmtMoney(v)} sub={ov.price_date ? `估值日 ${ov.price_date}` : '盘中快照实时估算'} icon={Wallet} iconCls="text-accent" />
          <StatCard label="现金" numeric={ov.cash} numFormat={v => fmtMoney(v)} icon={Banknote} iconCls="text-muted" />
          <StatCard label="持仓市值" numeric={ov.market_value} numFormat={v => fmtMoney(v)} icon={PieChart} iconCls="text-muted" />
          <StatCard
            label="累计盈亏"
            numeric={ov.total_pnl} numFormat={v => `${v >= 0 ? '+' : ''}${fmtMoney(v)} (${fmtPct(pnlPct / 100)})`}
            valueClass={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
            icon={TrendingUp}
            iconCls={priceColorClass((ov.total_pnl ?? 0) / (ov.initial_cash || 1))}
          />
        </div>

        <div className="mt-4">
          <PaperLiveConsole
            account={acc}
            ov={ov}
            orders={allOrders}
            fills={allFills}
            symbolNames={symbolNames}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[1fr_320px]">
          {/* 左列: 净值 + 持仓 + 流水 */}
          <div className="min-w-0 space-y-4">
            <div className="tf-panel rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">净值曲线 <span className="ml-1 text-[10px] text-muted">按交易日收盘定版</span></div>
              {nav.length > 0 ? <NavChart nav={nav} market={ov.market ?? 'cn'} /> : <div className="py-10 text-center text-xs text-muted">暂无定版净值 — 交易日盘后管道自动结算</div>}
            </div>

            <div className="tf-panel rounded-card border border-border bg-surface p-4">
              <div className="text-sm font-medium">当前持仓</div>
              {holdings.length === 0 ? (
                <div className="py-8 text-center text-xs text-muted">暂无持仓 — 右侧下单或等自动跟单触发</div>
              ) : (
                <table className="mt-2 w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-left text-[10px] text-muted">
                      <th className="py-1.5 font-normal">代码</th>
                      <th className="py-1.5 text-right font-normal">数量</th>
                      <th className="py-1.5 text-right font-normal">可卖(T+1)</th>
                      <th className="py-1.5 text-right font-normal">成本</th>
                      <th className="py-1.5 text-right font-normal">现价</th>
                      <th className="py-1.5 text-right font-normal">市值</th>
                      <th className="py-1.5 text-right font-normal">盈亏</th>
                      <th className="py-1.5 text-right font-normal">回放</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {holdings.map(h => (
                      <tr key={h.symbol} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                        <td className="py-1.5 font-sans">
                          {h.symbol}
                          {symbolNames[h.symbol] && (
                            <span className="ml-1.5 text-[10px] text-muted">{symbolNames[h.symbol]}</span>
                          )}
                        </td>
                        <td className="py-1.5 text-right">{h.qty}</td>
                        <td className="py-1.5 text-right text-muted">{h.available_qty}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.avg_cost, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.last_price, 3)}</td>
                        <td className="py-1.5 text-right">{fmtMoney(h.market_value, 0)}</td>
                        <td className={cn('py-1.5 text-right', priceColorClass(h.pnl))}>
                          {(h.pnl >= 0 ? '+' : '') + fmtMoney(h.pnl, 0)} ({fmtPct(h.pnl_pct / 100)})
                        </td>
                        <td className="py-1.5 text-right">
                          <button
                            onClick={() => setReplaySymbol(h.symbol)}
                            className="rounded-btn border border-border px-1.5 py-0.5 font-sans text-[10px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
                            title="在日K上回放该标的的买卖点"
                          >
                            回放
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {/* 订单 / 成交 / 回合明细 */}
            <div className="tf-panel rounded-card border border-border bg-surface p-4">
              <div className="flex flex-wrap items-center gap-2">
                {(['orders', 'trades', 'rounds'] as const).map(t => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={cn('flex items-center gap-1 rounded-btn px-2.5 py-1 text-xs transition-colors', tab === t ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary')}
                  >
                    {t === 'orders' && <><History className="h-3 w-3" />订单 {allOrders.length > 0 && <span className="text-[10px] text-muted">{allOrders.length}</span>}</>}
                    {t === 'trades' && <>成交台账 {allFills.length > 0 && <span className="text-[10px] text-muted">{allFills.length}</span>}</>}
                    {t === 'rounds' && <><ListChecks className="h-3 w-3" />回合明细 {rounds.length > 0 && <span className="text-[10px] text-muted">{rounds.length}</span>}</>}
                  </button>
                ))}
                {tab === 'orders' && allOrders.length > 20 && (
                  <button
                    onClick={() => setShowAllOrders(v => !v)}
                    className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent"
                  >
                    {showAllOrders ? '收起' : `显示全部 ${allOrders.length} 条`}
                  </button>
                )}
                {tab === 'trades' && allFills.length > 30 && (
                  <button
                    onClick={() => setShowAllTrades(v => !v)}
                    className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent"
                  >
                    {showAllTrades ? '收起' : `显示全部 ${allFills.length} 条`}
                  </button>
                )}
                {tab === 'trades' && (
                  <button
                    onClick={exportTradesCsv}
                    disabled={allFills.length === 0}
                    className="ml-auto rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-accent/30 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
                    title="导出全部成交台账 (CSV, Excel 可直接打开)"
                  >
                    导出 CSV
                  </button>
                )}
                {stats && (
                  <span className="ml-auto text-[11px] text-muted" title="回合为 FIFO 配对的完整买卖; 回撤按定版净值序列">
                    回合 {stats.rounds} · 胜率 {stats.win_rate}% · 盈亏比 {stats.profit_loss_ratio ?? '--'} · 均持 {stats.avg_holding_days}天 · 回撤{' '}
                    {stats.max_drawdown != null ? (stats.max_drawdown > 0 ? `-${stats.max_drawdown}%` : '0%') : '--'} ·{' '}
                    <span className={cn('font-mono', priceColorClass(stats.realized_pnl))}>
                      已实现 {stats.realized_pnl >= 0 ? '+' : ''}{fmtMoney(stats.realized_pnl, 0)}
                    </span>
                  </span>
                )}
              </div>
              {tab === 'orders' ? (
                <div className="mt-2 space-y-1">
                  {orders.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无订单</div>}
                  {orders.map((o: PaperOrder) => (
                    <div key={o.id} className="flex items-center gap-2 rounded-btn px-2 py-1.5 text-xs hover:bg-elevated/50">
                      <span className={cn('w-8 shrink-0 font-medium', o.side === 'buy' ? 'text-bull' : 'text-bear')}>
                        {o.side === 'buy' ? '买入' : '卖出'}
                      </span>
                      <span className="w-24 shrink-0 font-mono">{o.symbol}</span>
                      {symbolNames[o.symbol] && (
                        <span className="w-24 shrink-0 truncate text-[11px] text-muted" title={symbolNames[o.symbol]}>{symbolNames[o.symbol]}</span>
                      )}
                      <span className="w-16 shrink-0 font-mono text-right">{o.qty}</span>
                      <span className="w-16 shrink-0 text-muted">{ORDER_TYPE_LABEL[o.order_type]}</span>
                      <span className={cn(
                        'w-16 shrink-0 rounded-full px-1.5 py-px text-center text-[10px] leading-4',
                        o.status === 'filled' ? 'bg-bear/10 text-bear'
                          : o.status === 'expired' ? 'bg-warning/10 text-warning'
                          : o.status === 'cancelled' ? 'bg-elevated text-muted'
                          : 'bg-accent/10 text-accent',
                      )}>
                        {STATUS_LABEL[o.status]}
                      </span>
                      {o.status === 'pending' && o.postponed > 0 && (
                        <span className="shrink-0 rounded-full bg-accent/10 px-1.5 py-px text-[10px] leading-4 text-accent" title={`涨跌停排队重试中, 已顺延 ${o.postponed}/${3} 日`}>
                          排队 {o.postponed}/3
                        </span>
                      )}
                      <span className="min-w-0 flex-1 truncate text-[11px] text-muted" title={o.reason ?? undefined}>
                        {o.status === 'filled' && o.fill_price != null ? `@ ${fmtMoney(o.fill_price, 3)}` : o.reason ?? ''}
                      </span>
                      {o.status === 'pending' && (
                        <button onClick={() => cancelM.mutate(o.id)} className="shrink-0 rounded p-0.5 text-muted hover:text-foreground" title="撤单">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              ) : tab === 'trades' ? (
                <div className="mt-2 space-y-1">
                  {trades.length === 0 && <div className="py-6 text-center text-xs text-muted">暂无成交</div>}
                  {trades.map((f: PaperFill) => (
                    <div key={`${f.seq}-${f.order_id ?? 'corp'}`} className="flex items-center gap-2 rounded-btn px-2 py-1.5 font-mono text-xs hover:bg-elevated/50">
                      <span className="w-14 shrink-0 text-muted">{f.date}</span>
                      {f.kind === 'corp_action' ? (
                        <span className="text-accent">除权 ×{f.factor} ({f.symbol}: {f.qty_before} → )</span>
                      ) : (
                        <>
                          <span className={cn('w-8 shrink-0 font-sans font-medium', f.side === 'buy' ? 'text-bull' : 'text-bear')}>
                            {f.side === 'buy' ? '买入' : '卖出'}
                          </span>
                          <span className="w-24 shrink-0">{f.symbol}</span>
                          {symbolNames[f.symbol] && (
                            <span className="w-24 shrink-0 truncate font-sans text-[11px] text-muted" title={symbolNames[f.symbol]}>{symbolNames[f.symbol]}</span>
                          )}
                          <span className="w-16 shrink-0 text-right">{f.qty}</span>
                          <span className="w-20 shrink-0 text-right">@ {fmtMoney(f.price, 3)}</span>
                          <span className="w-16 shrink-0 text-right text-muted">费 {fmtMoney(f.fee)}</span>
                          <button
                            onClick={() => setReplaySymbol(f.symbol)}
                            className="ml-auto shrink-0 rounded-btn border border-border px-1.5 py-0.5 font-sans text-[10px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
                            title="在日K上回放该标的的买卖点"
                          >
                            回放
                          </button>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2">
                  {rounds.length === 0 ? (
                    <div className="py-6 text-center text-xs text-muted">暂无完整回合 — FIFO 配对买卖后在此展示每笔回合的盈亏明细</div>
                  ) : (
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-[10px] text-muted">
                          <th className="py-1.5 font-normal">标的</th>
                          <th className="py-1.5 text-right font-normal">数量</th>
                          <th className="py-1.5 text-right font-normal">开仓日</th>
                          <th className="py-1.5 text-right font-normal">平仓日</th>
                          <th className="py-1.5 text-right font-normal">持有天数</th>
                          <th className="py-1.5 text-right font-normal">回合盈亏</th>
                          <th className="py-1.5 text-right font-normal">收益率</th>
                        </tr>
                      </thead>
                      <tbody className="font-mono">
                        {rounds.map((rt, i) => (
                          <tr key={`${rt.symbol}-${rt.open_date}-${rt.close_date}-${i}`} className="border-t border-border/50 transition-colors hover:bg-elevated/40">
                            <td className="py-1.5 font-sans">
                              {rt.symbol}
                              {symbolNames[rt.symbol] && <span className="ml-1.5 text-[10px] text-muted">{symbolNames[rt.symbol]}</span>}
                            </td>
                            <td className="py-1.5 text-right">{rt.qty}</td>
                            <td className="py-1.5 text-right text-muted">{rt.open_date}</td>
                            <td className="py-1.5 text-right text-muted">{rt.close_date}</td>
                            <td className="py-1.5 text-right text-secondary">{rt.holding_days}天</td>
                            <td className={cn('py-1.5 text-right', priceColorClass(rt.pnl))}>
                              {(rt.pnl >= 0 ? '+' : '') + fmtMoney(rt.pnl, 0)}
                            </td>
                            <td className={cn('py-1.5 text-right', priceColorClass(rt.pnl_pct))}>
                              {fmtPct(rt.pnl_pct / 100)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* 右列: 下单 + 自动跟单 + 策略对比 */}
          <div className="space-y-4">
            <StrategyExecutionPanel key={acc} account={acc} />
            <OrderForm acc={acc} holdings={holdings} onDone={invalidateAll} />
            <details className="rounded-card border border-border bg-surface p-3">
              <summary className="cursor-pointer text-xs text-muted">旧事件跟单（兼容保留）</summary>
              <p className="my-2 text-[11px] leading-relaxed text-muted">旧规则按监控通知与池进出事件执行，不使用回测参数快照。策略买卖点跟单请使用上方入口，同一账户请只启用一种方式。</p>
              <AutoRulesPanel acc={acc} />
            </details>
            <CandidateCompareCard paper={{
              total_return_pct: nav.length >= 2 && nav[0].nav > 0 ? (nav[nav.length - 1].nav / nav[0].nav - 1) * 100 : null,
              annual_pct: nav.length >= 2 && nav[0].nav > 0
                ? (Math.pow(nav[nav.length - 1].nav / nav[0].nav, 252 / nav.length) - 1) * 100
                : null,
              max_drawdown: stats?.max_drawdown ?? null,
              win_rate: stats?.win_rate ?? null,
              n_trades: allFills.filter(f => f.kind !== 'corp_action').length,
              profit_loss_ratio: stats?.profit_loss_ratio ?? null,
            }} />
            <div className="rounded-card border border-border/60 bg-base/40 p-3 text-[11px] leading-relaxed text-muted">
              <div className="font-medium text-secondary">撮合口径</div>
              <div className="mt-1">· 即时单: 交易时段按最新快照价 + 滑点成交</div>
              <div>· 次日开盘 / 当日收盘单: 盘后管道按真实开盘/收盘价成交</div>
              <div>· {ov.market === 'cn' ? 'T+1: 当日买入次一交易日可卖' : 'T+0: 当日买入即可卖出'}</div>
              <div>· 停牌顺延 3 日自动过期; 除权按因子调整数量与成本, 台账留痕</div>
              <div className="mt-2 flex items-center justify-between border-t border-border/40 pt-2">
                <span title="开启后, 触及涨停的买入/触及跌停的卖出不再直接过期, 转次日开盘重试 (最多顺延 3 日)">
                  涨跌停排队次日重试
                </span>
                <button
                  onClick={() => queueM.mutate(!ov.queue_limit_orders)}
                  disabled={queueM.isPending}
                  className={cn(
                    'relative h-4 w-8 shrink-0 rounded-full transition-colors',
                    ov.queue_limit_orders ? 'bg-accent' : 'bg-border',
                  )}
                  aria-label="涨跌停排队次日重试开关"
                >
                  <span className={cn(
                    'absolute top-0.5 h-3 w-3 rounded-full bg-white shadow transition-all',
                    ov.queue_limit_orders ? 'left-[18px]' : 'left-0.5',
                  )} />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      {feeOpen && ov.fees && (
        <FeeSettingsModal
          accId={acc}
          fees={ov.fees}
          queue={!!ov.queue_limit_orders}
          onSaved={() => setFeeOpen(false)}
          onClose={() => setFeeOpen(false)}
        />
      )}
      {deleteOpen && (
        <DeleteAccountModal
          acc={acc}
          name={name}
          holdingsCount={holdings.length}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false)
            qc.invalidateQueries({ queryKey: QK.paperAll })
            onDeleted?.()
          }}
        />
      )}
      {replaySymbol && (
        <PaperReplayModal acc={acc} symbol={replaySymbol} onClose={() => setReplaySymbol(null)} />
      )}
    </div>
  )
}
