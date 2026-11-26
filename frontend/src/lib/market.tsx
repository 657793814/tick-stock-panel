// 全局市场切换（多市场扩展）：cn | hk | us
// Layout 顶部切换器 + localStorage 持久化，各页面通过 useMarket() 读取。
import { createContext, useContext, useState, type ReactNode } from 'react'
import { storage } from '@/lib/storage'

export type Market = 'cn' | 'hk' | 'us'

const MARKET_LABELS: Record<Market, string> = { cn: 'A股', hk: '港股', us: '美股' }

interface MarketCtx {
  market: Market
  setMarket: (m: Market) => void
  label: string
}

const Ctx = createContext<MarketCtx>({ market: 'cn', setMarket: () => {}, label: 'A股' })

export function MarketProvider({ children }: { children: ReactNode }) {
  const [market, setMarketState] = useState<Market>(() => storage.globalMarket.get('cn'))
  const setMarket = (m: Market) => {
    setMarketState(m)
    storage.globalMarket.set(m)
  }
  return (
    <Ctx.Provider value={{ market, setMarket, label: MARKET_LABELS[market] }}>
      {children}
    </Ctx.Provider>
  )
}

export function useMarket(): MarketCtx {
  return useContext(Ctx)
}

export function marketLabel(m: Market): string {
  return MARKET_LABELS[m]
}

/** symbol → 市场 (与后端 app.markets.market_of 同口径: 先看后缀, 再按代码首字符兜底)。 */
export function marketOfSymbol(symbol: string): Market {
  const s = symbol.trim().toUpperCase()
  if (s.endsWith('.SH') || s.endsWith('.SZ') || s.endsWith('.BJ')) return 'cn'
  if (s.endsWith('.HK')) return 'hk'
  if (s.endsWith('.US')) return 'us'
  const code = s.split('.')[0]
  return code && /^\d/.test(code) ? 'cn' : 'us'
}

/** 标的池按市场过滤, 避免港美股标的混进 A 股回测/自选/梯队。 */
export function filterSymbolsByMarket(symbols: string[], market: Market): string[] {
  return symbols.filter(s => marketOfSymbol(s) === market)
}
