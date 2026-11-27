/**
 * 东方财富日频事件表的列规范 —— 落盘列名 → 项目统一语义词。
 *
 * 列名来自 data/financials/{lhb_metrics,margin_metrics}/part.parquet
 * (见 services/eastmoney_sync.py + plugins/eastmoney/client.py):
 * 金额单位元, `*_ratio` 为百分数 (55.3 表示 55.3%), `announce_date` 是交易日。
 * 与 XTick 列规范同源, 复用 lib/field-labels 的词典与格式化, 不另起一套文案。
 */
import { fieldSpec, type FieldSpec } from '@/lib/field-labels'

const c = (key: string, extra?: Partial<Omit<FieldSpec, 'key'>>): FieldSpec => fieldSpec(key, extra)

export const EASTMONEY_TABLE_COLUMNS: Record<string, FieldSpec[]> = {
  lhb_metrics: [
    c('symbol'),
    c('announce_date'),
    c('lhb_net_amount'),
    c('lhb_net_ratio'),
    c('lhb_buy_amount'),
    c('lhb_sell_amount'),
    c('lhb_amount'),
    c('lhb_on_board'),
    c('lhb_reason_count'),
  ],
  margin_metrics: [
    c('symbol'),
    c('announce_date'),
    c('margin_balance'),
    c('margin_balance_ratio'),
    c('margin_buy_amount'),
    c('margin_buy_ratio'),
    c('margin_net_amount'),
    c('margin_net_5d'),
    c('margin_net_5d_ratio'),
    c('margin_short_balance'),
    c('margin_short_ratio'),
    c('float_mv'),
  ],
}

/** 表 → 因子口径说明 (只列语义词; 因子 id 放 title, 不占正文) */
export const FACTOR_HINTS: Record<string, { label: string; factor: string }[]> = {
  lhb_metrics: [
    { label: '龙虎榜净买占比', factor: 'lhb_net_ratio' },
    { label: '当日是否上榜', factor: 'lhb_on_board' },
    { label: '上榜原因条数', factor: 'lhb_reason_count' },
  ],
  margin_metrics: [
    { label: '融资余额占比', factor: 'margin_balance_ratio' },
    { label: '融资买入占比', factor: 'margin_buy_ratio' },
    { label: '5日融资净买占比', factor: 'margin_net_5d_ratio' },
    { label: '融券余额占比', factor: 'margin_short_ratio' },
  ],
}

export function eastmoneyColumns(table: string): FieldSpec[] {
  return EASTMONEY_TABLE_COLUMNS[table] ?? [c('symbol'), c('announce_date')]
}
