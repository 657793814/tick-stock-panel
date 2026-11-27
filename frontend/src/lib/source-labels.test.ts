import { describe, expect, it } from 'vitest'

import { joinSources, providerLabel, upstreamLabel } from './source-labels'

describe('source-labels: 数据源展示名', () => {
  it('插件 id 走统一展示名, 未登记 id 原样暴露', () => {
    expect(providerLabel('fuyao')).toBe('同花顺')
    expect(providerLabel('eastmoney')).toBe('东方财富')
    expect(providerLabel('tencent')).toBe('腾讯行情')
    expect(providerLabel('unknown_source')).toBe('unknown_source')
  })

  it('拉取地址按域名反查上游, 不看路径', () => {
    expect(upstreamLabel('https://fuyao.aicubes.cn/api/a-share-index/catalog/ths-index-list')).toBe('同花顺')
    expect(upstreamLabel('https://push2.eastmoney.com/api/qt/clist/get')).toBe('东方财富')
    expect(upstreamLabel('http://api.xtick.top/doc/hot/board')).toBe('XTick')
  })

  it('域名后缀冒充与非 http 地址不识别', () => {
    expect(upstreamLabel('https://fuyao.aicubes.cn.evil.com/x')).toBeNull()
    expect(upstreamLabel('not-a-url')).toBeNull()
    expect(upstreamLabel(null)).toBeNull()
  })

  it('多来源去重合并', () => {
    expect(joinSources(['同花顺', '同花顺', null, '东方财富'])).toBe('同花顺 / 东方财富')
    expect(joinSources([null, undefined])).toBe('未知来源')
  })
})
