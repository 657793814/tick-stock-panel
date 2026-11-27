import { describe, expect, it } from 'vitest'

import {
  alignOf,
  fieldSpec,
  formatFieldValue,
  normalizeKey,
  resolveColumns,
} from './field-labels'

describe('field-labels: 统一语义词', () => {
  it('归一化字段键: 下划线/连字符/大小写视为同一字段', () => {
    expect(normalizeKey('change_pct')).toBe('changepct')
    expect(normalizeKey('changePct')).toBe('changepct')
    expect(normalizeKey('pre-close')).toBe('preclose')
  })

  it('已知字段取项目语义词, 未知字段保留原始键名且不猜单位', () => {
    expect(fieldSpec('symbol').label).toBe('标的')
    expect(fieldSpec('pre_close')).toMatchObject({ label: '昨收', unit: 'price' })
    expect(fieldSpec('changePct')).toMatchObject({ label: '涨跌幅', unit: 'ratio' })
    expect(fieldSpec('lhb_net_ratio')).toMatchObject({ label: '净买额占成交额', unit: 'percent' })
    expect(fieldSpec('unknown_col')).toMatchObject({ label: 'unknown_col', unit: 'text' })
  })

  it('页面列规范可覆盖词典口径 (同一键不同接口语义不同)', () => {
    // 竞价接口 jjl = 竞价量, 情绪接口 jjl = 打板成功率
    expect(fieldSpec('jjl').label).toBe('竞价量')
    const emotion = fieldSpec('jjl', { label: '打板成功率', unit: 'percent' })
    expect(emotion).toMatchObject({ label: '打板成功率', unit: 'percent' })
  })
})

describe('field-labels: 单位格式化', () => {
  it('金额/成交量用万·亿缩写, 百分比区分小数与百分数口径', () => {
    expect(formatFieldValue(1_317_776_260, fieldSpec('amount'))).toBe('13.18亿')
    expect(formatFieldValue(8_957_430, fieldSpec('volume'))).toBe('895.74万')
    // 净额常为负: 负号保留, 量级同样缩写 (不能退化成 -341617877)
    expect(formatFieldValue(-341_617_877.31, fieldSpec('lhb_net_amount'))).toBe('-3.42亿')
    // ratio: 0.0123 = 1.23%
    expect(formatFieldValue(0.0123, fieldSpec('change_pct'))).toBe('+1.23%')
    // percent: 17.86 已是百分数
    expect(formatFieldValue(17.86, fieldSpec('zbl'))).toBe('17.86%')
  })

  it('计数/价格/日期/时间戳按统一口径渲染', () => {
    expect(formatFieldValue(12345, fieldSpec('holder'))).toBe('1.23万')
    expect(formatFieldValue(14.7, fieldSpec('price'))).toBe('14.70')
    expect(formatFieldValue('2026-09-11T00:00:00', fieldSpec('reportDate'))).toBe('2026-09-11')
    expect(formatFieldValue(1773676800000, fieldSpec('time'))).not.toBe('—')
  })

  it('空值与枚举值有明确语义', () => {
    expect(formatFieldValue(null, fieldSpec('price'))).toBe('—')
    expect(formatFieldValue(1, fieldSpec('flag'))).toBe('涨停')
    expect(formatFieldValue(0, fieldSpec('flag'))).toBe('炸板')
    expect(formatFieldValue(-1, fieldSpec('trend'))).toBe('主动卖')
    expect(formatFieldValue(1, fieldSpec('lhb_on_board'))).toBe('是')
    expect(formatFieldValue([{ a: 1 }, { a: 2 }], fieldSpec('buyTop5'))).toBe('共 2 条')
  })
})

describe('field-labels: 列解析与对齐', () => {
  it('按声明顺序取实际存在的列, 未声明字段追加在末尾', () => {
    const rows = [
      { code: '000001', amount: 100, moonlight: 'x' },
      { code: '000002', amount: 200 },
    ]
    const columns = resolveColumns(rows, [
      fieldSpec('code'),
      fieldSpec('price'),
      fieldSpec('amount'),
    ])
    expect(columns.map(column => column.key)).toEqual(['code', 'amount', 'moonlight'])
  })

  it('数值列默认右对齐, 文本列左对齐', () => {
    expect(alignOf(fieldSpec('amount'))).toBe('right')
    expect(alignOf(fieldSpec('code'))).toBe('left')
    expect(alignOf(fieldSpec('price', { align: 'left' }))).toBe('left')
  })
})
