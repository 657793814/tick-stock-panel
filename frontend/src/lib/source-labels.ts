/**
 * 数据来源展示名 —— 一套命名口径, 供扩展页 / 板块页 / 轮动卡共用。
 *
 * 两个来源:
 * 1. 插件 id (`fuyao` / `eastmoney` / `tencent` / `xtick` ...) 的展示名,
 *    与各插件 plugin.yaml 的 display_name 保持一致 (这里只做前端兜底,
 *    设置页仍以接口返回的 display_name 为准)。
 * 2. 扩展表的拉取地址 (pull.url) 反查上游名称 —— 概念/行业这类数据,
 *    真正供数的是「扩展表配的是哪个上游」, 而不是写死某个插件。
 *    用户把扩展表换成自有源时, 标签会如实变成"自有源/未知来源", 不冒充同花顺。
 */

/** 插件 id → 展示名 (与 plugin.yaml display_name 对齐) */
export const PROVIDER_LABELS: Record<string, string> = {
  tickflow: 'TickFlow',
  fuyao: '同花顺',
  tencent: '腾讯行情',
  eastmoney: '东方财富',
  xtick: 'XTick',
  tdxquant: 'TdxQuant',
}

/** 上游域名 → 展示名; 按顺序匹配 (先精确后包含), 用域名后缀判定, 不看路径 */
const HOST_LABELS: { host: string; label: string }[] = [
  { host: 'fuyao.aicubes.cn', label: '同花顺' },
  { host: 'eastmoney.com', label: '东方财富' },
  { host: 'gtimg.cn', label: '腾讯行情' },
  { host: 'xtick.top', label: 'XTick' },
  { host: 'tickflow.org', label: 'TickFlow' },
  { host: 'shy313.com', label: '自维护种子源' },
]

/** 插件 id → 展示名; 未知 id 原样返回, 便于暴露未登记的来源 */
export function providerLabel(id: string | null | undefined): string {
  if (!id) return '未知来源'
  return PROVIDER_LABELS[id.toLowerCase()] ?? id
}

/** 拉取地址 → 上游展示名; 非 http(s) 或无匹配返回 null (调用方自行降级) */
export function upstreamLabel(url: string | null | undefined): string | null {
  if (!url) return null
  let host: string
  try {
    host = new URL(url).hostname.toLowerCase()
  } catch {
    return null
  }
  const hit = HOST_LABELS.find(item => host === item.host || host.endsWith(`.${item.host}`))
  return hit?.label ?? null
}

/** 多来源去重合并: ['同花顺','同花顺',null] → '同花顺' */
export function joinSources(labels: (string | null | undefined)[]): string {
  const unique = [...new Set(labels.filter((label): label is string => !!label))]
  return unique.length ? unique.join(' / ') : '未知来源'
}
