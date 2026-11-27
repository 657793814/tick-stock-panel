/**
 * XTick 各接口的列规范 —— 页面只声明「展示哪些列、用什么语义词」。
 *
 * 字段名与口径来自 XTick 官方 API 文档 (references/apidoc.md, 2026-11 版):
 * 竞价 /doc/core/bidtime、连板天梯 /doc/hot/board、市场情绪 /doc/hot/emotion、
 * 资金流 /doc/hot/timemoney、龙虎榜 /doc/order/lhb、资讯 /doc/hot/news、
 * 日K /doc/kline/market、财务 /doc/gaap、股东 /doc/topholder|topflowholder|holdernum。
 *
 * 注意: 同一个键在不同接口语义不同 (如 `jjl`: 竞价接口是竞价量, 情绪接口是打板成功率),
 * 所以列规范按 kind 分别声明, 不做全局猜测; 未声明的新字段由 resolveColumns 以原始
 * 键名追加在末尾, 上游新增字段不会静默丢失。
 */
import { fieldSpec, type FieldSpec } from '@/lib/field-labels'

const c = (key: string, extra?: Partial<Omit<FieldSpec, 'key'>>): FieldSpec => fieldSpec(key, extra)

/** kind → 页面用语 (替代直接展示接口名 bid_time / top_flow_holder) */
export const XTICK_KIND_LABELS: Record<string, string> = {
  bid_time: '集合竞价',
  board: '连板天梯',
  emotion: '市场情绪',
  moneyflow: '资金流',
  lhb: '龙虎榜',
  news: '资讯',
  kline: '日K线',
  gaap: '财务指标',
  top_holder: '十大股东',
  top_flow_holder: '十大流通股东',
  holder_num: '股东户数',
}

export const XTICK_COLUMNS: Record<string, FieldSpec[]> = {
  bid_time: [
    c('code'),
    c('price'),
    c('close', { label: '昨收' }),
    c('jjzf'),
    c('jjl'),
    c('jje'),
    c('nol'),
    c('noe'),
    c('trend'),
    c('seq'),
    c('time'),
  ],
  board: [
    c('code'),
    c('price', { label: '最新价' }),
    c('preClose', { label: '昨收' }),
    c('days'),
    c('flag'),
    c('count'),
    c('time', { label: '交易时间' }),
    c('ftime'),
    c('bv1', { label: '封单量(买一)', hint: '涨停时为买一量, 即封单量' }),
    c('bp1'),
    c('sv1', { label: '卖一量', hint: '跌停时为卖一量, 即封单量' }),
    c('sp1'),
    c('volume'),
    c('amount'),
  ],
  emotion: [
    c('time', { label: '交易日', unit: 'date' }),
    c('lastTime', { label: '上一交易日', unit: 'date' }),
    c('ztnum'),
    c('dtnum'),
    c('ztlbnum'),
    c('mhigh'),
    c('zbnum'),
    // 情绪接口的 jjl 是"总打板成功率"(%), 不是竞价量
    c('jjl', { label: '打板成功率', unit: 'percent' }),
    c('zbl'),
    c('jj1t2'), c('jj2t3'), c('jj3t4'), c('jj4t5'), c('jj5tn'),
    c('zb1t2'), c('zb2t3'), c('zb3t4'), c('zb4t5'), c('zb5tn'),
  ],
  moneyflow: [
    c('code'),
    c('time'),
    c('buynumber'), c('sellnumber'), c('transactionnumber'),
    c('buymostamount'), c('buybigamount'), c('buymediumamount'), c('buysmallamount'),
    c('sellmostamount'), c('sellbigamount'), c('sellmediumamount'), c('sellsmallamount'),
    c('buymostvolume'), c('buybigvolume'), c('buymediumvolume'), c('buysmallvolume'),
    c('sellmostvolume'), c('sellbigvolume'), c('sellmediumvolume'), c('sellsmallvolume'),
    c('updateTime'),
  ],
  lhb: [
    c('code'),
    c('time', { label: '交易时间', unit: 'date' }),
    c('explanation'),
    c('buyTop5'),
    c('sellTop5'),
  ],
  news: [
    c('platname'),
    c('time', { label: '发布时间' }),
    c('title'),
    c('info'),
    c('url'),
  ],
  kline: [
    c('time', { label: '交易日期', unit: 'date' }),
    c('open', { label: '今开' }),
    c('high'),
    c('low'),
    c('close'),
    c('preClose', { label: '昨收' }),
    c('volume'),
    c('amount'),
    c('code'),
  ],
  gaap: [
    c('reportDate'),
    c('publicDate'),
    c('bps'),
    c('basicEps'), c('dilutedEps'),
    c('ocfps'), c('dps'), c('fund'),
    c('roe', { label: 'ROE(净资产收益率)' }),
    c('grossProfit'), c('netProfit'), c('salesProfit'),
    c('revenueInc'), c('profitInc'), c('netProfitM'), c('netProfitA'),
    c('gearRatio'),
    // 财务接口的 turnover = 存货周转率, 与行情换手率不是一回事
    c('turnover', { label: '存货周转率', unit: 'text' }),
    c('prePay'), c('salesCash'), c('equity'),
  ],
  top_holder: [
    c('reportDate'),
    c('publicDate'),
    c('rank'),
    c('name', { label: '股东名称', truncate: 24 }),
    c('num'),
    c('ratio'),
    c('nature'),
    c('reason'),
    c('code'),
  ],
  top_flow_holder: [
    c('reportDate'),
    c('publicDate'),
    c('rank'),
    c('name', { label: '股东名称', truncate: 24 }),
    c('num'),
    c('ratio'),
    c('nature'),
    c('reason'),
    c('code'),
  ],
  holder_num: [
    c('reportDate'),
    c('publicDate'),
    c('holder', { label: '股东总户数(户)' }),
    c('holderA', { label: 'A股股东户数' }),
    c('holderFlow', { label: '流通股东户数' }),
    c('holderB'), c('holderH'), c('holderOther'),
    c('code'),
  ],
}

/** 未知 kind 的兜底: 用通用词典给已知字段上语义词, 其余保留原键名 */
export function columnsFor(kind: string): FieldSpec[] {
  return XTICK_COLUMNS[kind] ?? []
}
