/**
 * 统一字段语义词典 —— 外部数据源原始字段 → 项目统一语义词。
 *
 * 背景: 扩展页 (XTick / 东方财富) 的表格曾直接打印上游字段名
 * (如竞价接口的 `jjzf`、东财的 `lhb_net_ratio`), 与本项目其它列表
 * (标的 / 现价 / 涨跌幅 / 成交额) 的用词不一致。这里把「字段 → 中文标签 + 单位」
 * 收敛成一张表, 页面只声明自己的列顺序, 不再各写一套文案。
 *
 * 约定:
 * - 键统一经 normalizeKey 归一 (去掉 `_`/空格/`-` 并转小写), 因此
 *   `changePct` / `change_pct` / `change-pct` 视为同一字段。
 * - `unit` 决定数值格式化与右对齐; 未识别字段原样展示并保留原始键名,
 *   不猜单位 —— 口径不明的数字宁可原样也不编造。
 * - `ratio` = 0.0123 表示 1.23%; `percent` = 1.23 表示 1.23% (上游已是百分数)。
 *   二者不可混用, 否则会出现 123% 这类假数据。
 */
import { fmtVolume } from '@/lib/format'

export type FieldUnit =
  | 'text'      // 原样文本
  | 'price'     // 价格, 两位小数
  | 'change'    // 涨跌额, 带符号两位小数
  | 'amount'    // 金额 (元), 万/亿缩写
  | 'volume'    // 成交量 (手/股), 万/亿缩写
  | 'ratio'     // 小数比例 (0.0123 → 1.23%)
  | 'percent'   // 百分数 (1.23 → 1.23%)
  | 'count'     // 计数 (家数/户数/股数)
  | 'date'      // 日期 (YYYY-MM-DD)
  | 'datetime'  // 时间 (毫秒时间戳或字符串)
  | 'trend'     // 主动方向: -1 主动卖 / 0 中性 / 1 主动买
  | 'bool'      // 1/0 → 是/否

export interface FieldSpec {
  /** 原始字段键 (必须与上游返回一致) */
  key: string
  /** 项目统一语义词 */
  label: string
  /** 数值口径; 缺省 text */
  unit?: FieldUnit
  /** 对齐方式; 缺省按 unit 推断 (数值右对齐) */
  align?: 'left' | 'right'
  /** 涨跌语义色 (红涨绿跌), 仅用于涨跌幅/净额这类有方向的字段 */
  colored?: boolean
  /** 文本截断长度 (标题/摘要这类长文本) */
  truncate?: number
  /** 枚举值的语义文案 */
  valueLabels?: Record<string, string>
  /** 鼠标悬停说明 (口径/单位) */
  hint?: string
}

type FieldMeta = Omit<FieldSpec, 'key'>

/** 归一化字段键: 大小写、下划线、连字符、空格都视为同一字段 */
export function normalizeKey(key: string): string {
  return key.replace(/[\s_-]/g, '').toLowerCase()
}

// ---------------------------------------------------------------------------
// 通用词典: 行情 / 财务 / 股东 / 事件 四类字段
// 说明: 只收「口径确定」的字段; 同一键在不同源单位不同的, 由页面列规范显式覆盖。
// ---------------------------------------------------------------------------

const DICT: Record<string, FieldMeta> = {
  // ===== 标的 =====
  symbol: { label: '标的', unit: 'text' },
  code: { label: '代码', unit: 'text' },
  thscode: { label: '代码', unit: 'text' },
  name: { label: '名称', unit: 'text' },
  stockname: { label: '名称', unit: 'text' },
  market: { label: '市场', unit: 'text' },
  type: { label: '标的类型', unit: 'text', valueLabels: { '1': '沪深京A股', '2': '沪深指数', '3': '港股', '4': 'ETF基金', '5': '可转债' } },

  // ===== 行情 =====
  price: { label: '现价', unit: 'price' },
  close: { label: '收盘', unit: 'price' },
  preclose: { label: '昨收', unit: 'price' },
  open: { label: '开盘', unit: 'price' },
  high: { label: '最高', unit: 'price' },
  low: { label: '最低', unit: 'price' },
  changepct: { label: '涨跌幅', unit: 'ratio', colored: true },
  changeamount: { label: '涨跌额', unit: 'change', colored: true },
  volume: { label: '成交量', unit: 'volume' },
  amount: { label: '成交额', unit: 'amount' },
  turnoverrate: { label: '换手率', unit: 'ratio' },
  turnover: { label: '换手率', unit: 'ratio' },
  amplitude: { label: '振幅', unit: 'ratio' },
  volratio: { label: '量比', unit: 'text' },
  marketcap: { label: '总市值', unit: 'amount' },
  floatmarketcap: { label: '流通市值', unit: 'amount' },
  pe: { label: '市盈率', unit: 'text' },
  pb: { label: '市净率', unit: 'text' },
  limitup: { label: '涨停价', unit: 'price' },
  limitdown: { label: '跌停价', unit: 'price' },

  // ===== 时间 =====
  time: { label: '时间', unit: 'datetime' },
  date: { label: '日期', unit: 'date' },
  tradedate: { label: '交易日', unit: 'date' },
  announcedate: { label: '交易日', unit: 'date' },
  reportdate: { label: '报告期', unit: 'date' },
  publicdate: { label: '公告日', unit: 'date' },
  lasttime: { label: '上一交易日', unit: 'datetime' },
  updatetime: { label: '更新时间', unit: 'datetime' },
  periodend: { label: '报告期', unit: 'date' },

  // ===== 竞价 =====
  seq: { label: '竞价序列', unit: 'count' },
  jjzf: { label: '竞价涨幅', unit: 'percent', colored: true },
  jjl: { label: '竞价量', unit: 'volume' },
  jje: { label: '竞价金额', unit: 'amount' },
  nol: { label: '未匹配量', unit: 'volume' },
  noe: { label: '未匹配金额', unit: 'amount' },
  trend: { label: '主动方向', unit: 'trend' },

  // ===== 连板天梯 =====
  flag: { label: '状态', unit: 'text', valueLabels: { '0': '炸板', '1': '涨停', '2': '跌停' } },
  count: { label: '炸板次数', unit: 'count' },
  ftime: { label: '首次触板', unit: 'datetime' },
  days: { label: '连板天数', unit: 'count' },
  bv1: { label: '买一量 / 封单量', unit: 'volume' },
  sv1: { label: '卖一量 / 封单量', unit: 'volume' },
  bp1: { label: '买一价', unit: 'price' },
  sp1: { label: '卖一价', unit: 'price' },

  // ===== 市场情绪 =====
  ztlbnum: { label: '连板家数', unit: 'count' },
  ztnum: { label: '涨停家数', unit: 'count' },
  dtnum: { label: '跌停家数', unit: 'count' },
  zbnum: { label: '炸板家数', unit: 'count' },
  mhigh: { label: '连板高度', unit: 'count' },
  zbl: { label: '炸板率', unit: 'percent' },
  jj1t2: { label: '一进二晋级率', unit: 'percent' },
  jj2t3: { label: '二进三晋级率', unit: 'percent' },
  jj3t4: { label: '三进四晋级率', unit: 'percent' },
  jj4t5: { label: '四进五晋级率', unit: 'percent' },
  jj5tn: { label: '五板以上晋级率', unit: 'percent' },
  zb1t2: { label: '一进二炸板率', unit: 'percent' },
  zb2t3: { label: '二进三炸板率', unit: 'percent' },
  zb3t4: { label: '三进四炸板率', unit: 'percent' },
  zb4t5: { label: '四进五炸板率', unit: 'percent' },
  zb5tn: { label: '五板以上炸板率', unit: 'percent' },

  // ===== 资金流 =====
  buynumber: { label: '主买单数', unit: 'count' },
  sellnumber: { label: '主卖单数', unit: 'count' },
  transactionnumber: { label: '成交笔数', unit: 'count' },
  buymostamount: { label: '主买特大单额', unit: 'amount' },
  buybigamount: { label: '主买大单额', unit: 'amount' },
  buymediumamount: { label: '主买中单额', unit: 'amount' },
  buysmallamount: { label: '主买小单额', unit: 'amount' },
  sellmostamount: { label: '主卖特大单额', unit: 'amount' },
  sellbigamount: { label: '主卖大单额', unit: 'amount' },
  sellmediumamount: { label: '主卖中单额', unit: 'amount' },
  sellsmallamount: { label: '主卖小单额', unit: 'amount' },
  buymostvolume: { label: '主买特大单量', unit: 'volume' },
  buybigvolume: { label: '主买大单量', unit: 'volume' },
  buymediumvolume: { label: '主买中单量', unit: 'volume' },
  buysmallvolume: { label: '主买小单量', unit: 'volume' },
  sellmostvolume: { label: '主卖特大单量', unit: 'volume' },
  sellbigvolume: { label: '主卖大单量', unit: 'volume' },
  sellmediumvolume: { label: '主卖中单量', unit: 'volume' },
  sellsmallvolume: { label: '主卖小单量', unit: 'volume' },

  // ===== 新闻 =====
  platname: { label: '新闻源', unit: 'text' },
  platid: { label: '新闻源编号', unit: 'text' },
  id: { label: '编号', unit: 'text' },
  title: { label: '标题', unit: 'text', truncate: 46 },
  info: { label: '摘要', unit: 'text', truncate: 60 },
  url: { label: '链接', unit: 'text', truncate: 40 },
  explanation: { label: '上榜原因', unit: 'text', truncate: 40 },
  buytop5: { label: '买入前五席位', unit: 'text' },
  selltop5: { label: '卖出前五席位', unit: 'text' },

  // ===== 财务指标 (XTick /doc/gaap 口径; 同比与比率为百分数) =====
  ocfps: { label: '每股经营现金流', unit: 'price' },
  bps: { label: '每股净资产', unit: 'price' },
  basiceps: { label: '基本每股收益', unit: 'price' },
  dilutedeps: { label: '稀释每股收益', unit: 'price' },
  dps: { label: '每股未分配利润', unit: 'price' },
  fund: { label: '每股资本公积', unit: 'price' },
  equity: { label: '净资产收益率(权益口径)', unit: 'percent' },
  roe: { label: 'ROE', unit: 'percent' },
  salesprofit: { label: '销售毛利率', unit: 'percent' },
  grossprofit: { label: '毛利率', unit: 'percent' },
  netprofit: { label: '净利率', unit: 'percent' },
  revenueinc: { label: '营收同比', unit: 'percent', colored: true },
  profitinc: { label: '净利同比', unit: 'percent', colored: true },
  netprofitm: { label: '归母净利同比', unit: 'percent', colored: true },
  netprofita: { label: '扣非净利同比', unit: 'percent', colored: true },
  prepay: { label: '预收款', unit: 'amount' },
  salescash: { label: '销售现金流', unit: 'amount' },
  gearratio: { label: '资产负债率', unit: 'percent' },

  // ===== 股东 =====
  num: { label: '持股数(股)', unit: 'count' },
  ratio: { label: '持股比例', unit: 'percent' },
  nature: { label: '股份性质', unit: 'text' },
  reason: { label: '变动原因', unit: 'text', truncate: 28 },
  rank: { label: '排名', unit: 'count' },
  holder: { label: '股东总户数', unit: 'count' },
  holdera: { label: 'A股股东户数', unit: 'count' },
  holderb: { label: 'B股股东户数', unit: 'count' },
  holderh: { label: 'H股股东户数', unit: 'count' },
  holderflow: { label: '流通股东户数', unit: 'count' },
  holderother: { label: '未流通股东户数', unit: 'count' },
  holdernum: { label: '股东户数', unit: 'count' },

  // ===== 东方财富日频事件 (金额元, 比率为百分数) =====
  lhbnetamount: { label: '龙虎榜净买额', unit: 'amount', colored: true },
  lhbbuyamount: { label: '龙虎榜买入额', unit: 'amount' },
  lhbsellamount: { label: '龙虎榜卖出额', unit: 'amount' },
  lhbamount: { label: '当日成交额', unit: 'amount' },
  lhbnetratio: { label: '净买额占成交额', unit: 'percent', colored: true },
  lhbonboard: { label: '当日是否上榜', unit: 'bool' },
  lhbreasoncount: { label: '上榜原因条数', unit: 'count' },
  marginbalance: { label: '融资余额', unit: 'amount' },
  marginshortbalance: { label: '融券余额', unit: 'amount' },
  marginbuyamount: { label: '融资买入额', unit: 'amount' },
  marginnetamount: { label: '融资净买额', unit: 'amount', colored: true },
  marginnet5d: { label: '5日融资净买额', unit: 'amount', colored: true },
  floatmv: { label: '流通市值', unit: 'amount' },
  marginbalanceratio: { label: '融资余额占比', unit: 'percent' },
  marginbuyratio: { label: '融资买入占比', unit: 'percent' },
  marginnet5dratio: { label: '5日融资净买占比', unit: 'percent', colored: true },
  marginshortratio: { label: '融券余额占比', unit: 'percent' },
}

const NUMERIC_UNITS: ReadonlySet<FieldUnit> = new Set<FieldUnit>([
  'price', 'change', 'amount', 'volume', 'ratio', 'percent', 'count',
])

/** 按字段键取统一语义 (未收录的字段保留原始键名, 不猜单位) */
export function fieldSpec(key: string, override?: Partial<FieldMeta>): FieldSpec {
  const meta = DICT[normalizeKey(key)]
  if (meta) return { key, ...meta, ...override }
  return { key, label: key, unit: 'text', ...override }
}

/** 列对齐: 显式声明优先, 否则数值列右对齐 */
export function alignOf(spec: FieldSpec): 'left' | 'right' {
  if (spec.align) return spec.align
  return NUMERIC_UNITS.has(spec.unit ?? 'text') ? 'right' : 'left'
}

function signed(text: string, value: number): string {
  return value > 0 ? `+${text}` : text
}

function trimZeros(value: number): string {
  const text = value.toFixed(4)
  return text.replace(/0+$/, '').replace(/\.$/, '')
}

/** 金额 (元) → 万/亿缩写; 支持负值 (净买额/净流入常为负), 不能用只看正数的 fmtBigNum */
function formatAmount(value: number): string {
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value)
  if (abs >= 1e12) return `${sign}${(abs / 1e12).toFixed(2)}万亿`
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`
  return value.toFixed(0)
}

function formatTimestamp(ms: number, withTime: boolean): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  // 秒级时间戳 (10 位) 兼容: 上游偶尔返回秒
  const value = ms < 1e11 ? ms * 1000 : ms
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(ms)
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
  }).formatToParts(date)
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  const day = `${get('year')}-${get('month')}-${get('day')}`
  return withTime ? `${day} ${get('hour')}:${get('minute')}` : day
}

function formatNumber(value: number, spec: FieldSpec): string {
  switch (spec.unit) {
    case 'price':
      return value.toFixed(2)
    case 'change':
      return signed(value.toFixed(2), value)
    case 'amount':
      return formatAmount(value)
    case 'volume':
      return fmtVolume(value)
    case 'ratio':
      return `${signed((value * 100).toFixed(2), value)}%`
    case 'percent':
      return `${spec.colored ? signed(value.toFixed(2), value) : value.toFixed(2)}%`
    case 'count': {
      // 计数 (户数/股数/家数) 保留两位小数: 1.23万户 比 1万户 有信息量
      if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
      if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(2)}万`
      return value.toFixed(0)
    }
    case 'trend':
      return value > 0 ? '主动买' : value < 0 ? '主动卖' : '中性'
    case 'bool':
      return value ? '是' : '否'
    default:
      return Number.isInteger(value) ? String(value) : trimZeros(value)
  }
}

/** 单元格渲染: 单位口径与 null 语义统一在这里收口 */
export function formatFieldValue(value: unknown, spec: FieldSpec): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return `共 ${value.length} 条`
  if (typeof value === 'object') {
    const text = JSON.stringify(value)
    return text.length > 80 ? `${text.slice(0, 80)}…` : text
  }
  if (spec.valueLabels) {
    const mapped = spec.valueLabels[String(value)]
    if (mapped) return mapped
  }
  if (spec.unit === 'date' || spec.unit === 'datetime') {
    if (typeof value === 'number') return formatTimestamp(value, spec.unit === 'datetime')
    const text = String(value)
    return spec.unit === 'date' ? text.slice(0, 10) : text
  }
  if (typeof value === 'string') {
    const num = Number(value)
    // 上游偶发把数字包成字符串, 有单位口径时按数值渲染
    if (spec.unit && spec.unit !== 'text' && value.trim() !== '' && Number.isFinite(num)) {
      return formatNumber(num, spec)
    }
    return spec.truncate && value.length > spec.truncate
      ? `${value.slice(0, spec.truncate)}…`
      : value
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  return formatNumber(value, spec)
}

/**
 * 解析表格列: 按页面声明的顺序保留上游实际存在的列, 未声明的列追加在末尾
 * (保留原始键名并标注 title), 上游新增字段不会静默消失。
 */
export function resolveColumns(
  rows: Record<string, unknown>[],
  specs: FieldSpec[],
  limit?: number,
): FieldSpec[] {
  const present = new Set(rows.flatMap(row => Object.keys(row)))
  const known = new Set(specs.map(spec => spec.key))
  const ordered = specs.filter(spec => present.has(spec.key))
  const extras = [...present]
    .filter(key => !known.has(key))
    .map(key => fieldSpec(key))
  const columns = [...ordered, ...extras]
  return limit != null ? columns.slice(0, limit) : columns
}
