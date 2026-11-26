import { useEffect, useMemo, useState } from 'react'
import { api } from './api'

/**
 * 逐笔行情通道 (SSE)。
 *
 * 订阅一组标的, 后端在每次行情轮询 (1~3s) 后把最新快照推给本连接;
 * 连接建立时先推一帧, 休市时也能拿到最近收盘价作为兜底。
 * 上游是轮询快照, 不是交易所逐笔成交明细。
 */
export interface SymbolTick {
  symbol: string
  last_price: number
  prev_close?: number | null
  change_pct?: number | null
  ts: number
  /** 相对上一拍的价格方向, 用于闪烁动画 */
  dir: 'up' | 'down' | 'flat'
}

const A_SHARE = /^\d{6}\.(SH|SZ|BJ)$/

export function useSymbolTicks(symbols: string[], enabled = true): Record<string, SymbolTick> {
  // 数组按内容归一, 避免父组件每次渲染都重连 SSE
  const key = useMemo(
    () => Array.from(new Set(symbols.filter(Boolean).map(s => s.toUpperCase()))).sort().join(','),
    [symbols],
  )
  const [ticks, setTicks] = useState<Record<string, SymbolTick>>({})

  useEffect(() => {
    if (!enabled || !key) return
    const list = key.split(',')
    const es = new EventSource(`/api/intraday/ticks?symbols=${encodeURIComponent(key)}`)

    const onTicks = (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data) as {
          ts: number
          quotes: Array<Omit<SymbolTick, 'ts' | 'dir'>>
        }
        const quotes = payload.quotes ?? []
        if (!quotes.length) return
        setTicks(prev => {
          const next = { ...prev }
          for (const q of quotes) {
            if (typeof q.last_price !== 'number') continue
            const old = prev[q.symbol]
            const dir: SymbolTick['dir'] = !old
              ? 'flat'
              : q.last_price > old.last_price
                ? 'up'
                : q.last_price < old.last_price
                  ? 'down'
                  : 'flat'
            next[q.symbol] = { ...q, ts: payload.ts, dir }
          }
          return next
        })
      } catch {
        // 忽略解析错误, 保持上一拍价格
      }
    }
    es.addEventListener('ticks', onTicks as EventListener)

    // A 股标的登记需求租约 (港美股走各自行情服务, 不在这里登记)
    const demand = list.filter(s => A_SHARE.test(s))
    const clientId = `paper_ticks_${globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)}`
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const renew = () => {
      if (!active || !demand.length) return
      api.subscribeQuotes(clientId, demand)
        .catch(() => {
          // 租约失败只影响上游是否主动拉取, 不影响已推送的快照
        })
        .finally(() => {
          if (active) timer = setTimeout(renew, 20_000)
        })
    }
    renew()

    return () => {
      active = false
      clearTimeout(timer)
      es.removeEventListener('ticks', onTicks as EventListener)
      es.close()
      if (demand.length) void api.unsubscribeQuotes(clientId).catch(() => {})
    }
  }, [key, enabled])

  return ticks
}
