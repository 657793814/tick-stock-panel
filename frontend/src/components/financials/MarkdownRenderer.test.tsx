// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MarkdownRenderer } from './MarkdownRenderer'

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

async function renderToHtml(content: string): Promise<string> {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); host.remove() }
  await act(async () => root.render(<MarkdownRenderer content={content} />))
  return host.innerHTML
}

it('colors signed pct and limit words red-up/green-down (A股口径)', async () => {
  const html = await renderToHtml('今日上涨 **+2.35%**，昨日 -1.20%，盘中一度涨停。')
  // 上涨 / +2.35% (含加粗内) / 涨停 → 红; -1.20% → 绿
  expect(html.match(/text-bull/g)?.length).toBe(3)
  expect(html).toContain('text-bear')
  // 加粗内的百分比递归拿到语义色: strong 直接包裹着色 span
  expect(html).toMatch(/<strong[^>]*><span[^>]*text-bull[^>]*>\+2\.35%<\/span><\/strong>/)
  // 涨停词徽章化: 底色 + 着色
  expect(html).toMatch(/bg-bull\/10[^>]*>涨停<\/span>/)
})

it('does not color unsigned pct (direction unknown, e.g. turnover)', async () => {
  const html = await renderToHtml('换手率 3.66%，成交额 1.2亿元。')
  expect(html).not.toContain('text-bull')
  expect(html).not.toContain('text-bear')
  expect(html).toContain('3.66%')
  // 金额数字等宽, 单位留在 span 外
  expect(html).toMatch(/font-mono[^>]*>1\.2<\/span>亿元/)
})

it('colors direction words without background', async () => {
  const html = await renderToHtml('今日上涨，昨日下跌，量能走弱。')
  expect(html.match(/text-bull/g)?.length).toBe(1)
  expect(html.match(/text-bear/g)?.length).toBe(2)
})

it('leaves signed pct inside inline code untouched', async () => {
  const html = await renderToHtml('字段示例 `+1.00%` 为原始文本。')
  expect(html).not.toContain('text-bull')
  expect(html).toContain('<code')
})

it('renders Markdown links with formatted labels and safe external navigation', async () => {
  const html = await renderToHtml('阅读 [**接入文档**](https://example.com/docs)。')
  const host = document.createElement('div')
  host.innerHTML = html
  const link = host.querySelector('a')!
  expect(link.getAttribute('href')).toBe('https://example.com/docs')
  expect(link.querySelector('strong')?.textContent).toBe('接入文档')
  expect(link.target).toBe('_blank')
  expect(link.rel).toContain('noopener')
  expect(link.rel).toContain('noreferrer')
})

it('keeps unsafe links and raw HTML inert', async () => {
  const html = await renderToHtml('[运行](javascript:alert%281%29)\n<img src=x onerror=alert(1)>\n<script>alert(1)</script>')
  const host = document.createElement('div')
  host.innerHTML = html
  expect(host.querySelector('a, img, script')).toBeNull()
  expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
})

it('wide tables (>4 cols) use auto layout, nowrap cells and horizontal scroll', async () => {
  const html = await renderToHtml(
    '| # | 标的 | 收盘 | 当日 | 换手率 | 评分 |\n' +
    '|---|---|---|---|---|---|\n' +
    '| 1 | 金健米业 600127.SH | 7.72 | +1.05% | 3.66% | 82 |',
  )
  // 宽表: 按内容取宽 + 可横向滚动, 单元格不折行(标的/代码不再断行)
  expect(html).toContain('overflow-x-auto')
  expect(html).toContain('table-auto')
  expect(html).toContain('whitespace-nowrap')
  expect(html).not.toContain('table-fixed')
  expect(html).not.toContain('break-words')
})

it('narrow tables (<=4 cols) keep fixed layout with breakable cells', async () => {
  const html = await renderToHtml(
    '| 维度 | 数值 | 判断 |\n' +
    '|---|---|---|\n' +
    '| 营收 | 12.3亿元 | 同比上涨 |\n',
  )
  expect(html).toContain('table-fixed')
  expect(html).toContain('break-words')
  expect(html).not.toContain('overflow-x-auto')
})

it('joins consecutive plain lines into one paragraph (markdown soft wrap)', async () => {
  // YAML | 块按源码宽度折行的长文本: 连续普通行应合为一段, 而不是每行一个 <p>
  const html = await renderToHtml('路由数据集: 实时快照、五档盘口、\n日K 与全量分钟均为单标的或批量端点。\n\n第二段独立成段。')
  const host = document.createElement('div')
  host.innerHTML = html
  const ps = host.querySelectorAll('p')
  expect(ps.length).toBe(2)
  // 中文边界直接相连, 不插入空格
  expect(ps[0]!.textContent).toBe('路由数据集: 实时快照、五档盘口、日K 与全量分钟均为单标的或批量端点。')
  expect(ps[1]!.textContent).toBe('第二段独立成段。')
})

it('joins english lines with a space, CJK boundary without', async () => {
  const html = await renderToHtml('Visit the docs page\nfor more details。')
  const host = document.createElement('div')
  host.innerHTML = html
  expect(host.querySelector('p')!.textContent).toBe('Visit the docs page for more details。')
})

it('soft-wrap merge stops at block starts (heading/list/quote/table)', async () => {
  const html = await renderToHtml('第一段第一行\n第一段第二行\n## 标题\n- 列表项\n> 引用\n| 表 |\n|---|\n| a |')
  const host = document.createElement('div')
  host.innerHTML = html
  const ps = host.querySelectorAll('p')
  expect(ps.length).toBe(1)
  expect(ps[0]!.textContent).toBe('第一段第一行第一段第二行')
  expect(host.textContent).toContain('标题')
  expect(host.querySelector('li')).not.toBeNull()
  expect(host.querySelector('blockquote')).not.toBeNull()
  expect(host.querySelector('table')).not.toBeNull()
})

it('absorbs indented continuation lines into list items (yaml | block style)', async () => {
  // 腾讯行情描述的真实形态: 列表项后跟缩进续行, 续行应并入条目而非掉出成独立短段
  const html = await renderToHtml(
    '腾讯免费公开行情接口, 无需注册和 token。路由能力:\n' +
    '- 分钟 K (m1, 单标的拉取): 实测每次约 482 根 (≈2 交易日), 上游不接受日期范围参数,\n' +
    '  逐日增量落盘可累积历史;\n' +
    '- 实时快照 / 五档盘口 (批量 ≤50 只/请求): 按标的列表拉取,\n' +
    '  上游无法枚举全市场标的。\n' +
    '  自选补拉与指数快照正常可用;',
  )
  const host = document.createElement('div')
  host.innerHTML = html
  // 只有 1 个 <p> (引言行), 两条列表项各自吸收续行
  expect(host.querySelectorAll('p').length).toBe(1)
  const items = [...host.querySelectorAll('li')].map(li => li.textContent)
  expect(items.length).toBe(2)
  expect(items[0]).toBe('分钟 K (m1, 单标的拉取): 实测每次约 482 根 (≈2 交易日), 上游不接受日期范围参数,逐日增量落盘可累积历史;')
  expect(items[1]).toBe('实时快照 / 五档盘口 (批量 ≤50 只/请求): 按标的列表拉取,上游无法枚举全市场标的。自选补拉与指数快照正常可用;')
})

it('ordered list items also absorb indented continuations', async () => {
  const html = await renderToHtml('1. 第一条较长说明需要折行,\n   续行并入同一条目\n2. 第二条')
  const host = document.createElement('div')
  host.innerHTML = html
  const items = [...host.querySelectorAll('li')].map(li => li.textContent)
  // ol 的 li 内含序号徽章 (如 "1"), 文本断言需带上
  expect(items).toEqual(['1第一条较长说明需要折行,续行并入同一条目', '2第二条'])
})
