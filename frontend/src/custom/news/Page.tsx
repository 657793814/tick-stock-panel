/**
 * 新闻资讯页 —— 资讯详情/事件库、快讯、公告、热门专题、异动解读、个股分时新闻。
 *
 * 归属: 扩展域消费页 (官方), 经 custom/news/extension.tsx 注册路由与导航。
 * 数据全部来自后端 /api/news/*, 由 services.news 做 fuyao 降级与 Redis 7 天缓存;
 * 上游 HTML 正文按内容安全策略只取纯文本展示, 链接仅允许 http/https。
 */
import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { ExternalLink, Loader2, RefreshCw, Search, AlertTriangle } from 'lucide-react'

import {
  api,
  type NewsPayload,
  type NewsFlashItem,
  type NewsTopicItem,
  type NewsAnomalyItem,
  type NewsTimelineItem,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

type Tab = 'article' | 'flash' | 'notice' | 'topic' | 'anomaly' | 'timeline'

const TABS: { id: Tab; label: string }[] = [
  { id: 'article', label: '资讯' },
  { id: 'flash', label: '快讯' },
  { id: 'notice', label: '公告' },
  { id: 'topic', label: '热门专题' },
  { id: 'anomaly', label: '异动解读' },
  { id: 'timeline', label: '个股分时' },
]

const inputCls = 'h-8 rounded-md border border-border bg-elevated px-2 text-xs text-foreground outline-none focus:border-accent'
const btnCls = 'inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50 transition-colors'

function htmlText(html?: string | null): string {
  if (!html) return ''
  return (new DOMParser().parseFromString(html, 'text/html').body.textContent ?? '').trim()
}

function safeHref(url?: string | null): string | undefined {
  if (!url) return undefined
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined
  } catch {
    return undefined
  }
}

/** 快讯 encoded_url 末段即为 article/detail 可用的 encoded_seq（实测）。 */
function encodedSeqFromUrl(url?: string | null): string | undefined {
  const href = safeHref(url)
  if (!href) return undefined
  try {
    const token = new URL(href).pathname.split('/').filter(Boolean).pop()
    return token?.replace(/\.(shtml|html)$/i, '') || undefined
  } catch {
    return undefined
  }
}

function fmtMs(ms?: number | null): string {
  if (ms == null) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(ms))
}

function cnDate(offsetDays = 0): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(Date.now() + offsetDays * 86_400_000))
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

function Loading() {
  return <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" />加载中…</div>
}

type QueryLike<T> = Pick<UseQueryResult<NewsPayload<T>>,
  'data' | 'isLoading' | 'isFetching' | 'isError' | 'refetch'>

function NewsResult<T>({ query, children, emptyHint }: {
  query: QueryLike<T>
  children: (payload: NewsPayload<T>) => ReactNode
  emptyHint: string
}) {
  if (query.isLoading) return <Loading />
  if (query.isError) {
    return <ErrorState title="新闻加载失败" hint="请检查网络后重试；若提示缺少数据源，请到设置页配置扶摇 API Key" onRetry={() => void query.refetch()} />
  }
  const payload = query.data
  if (!payload) return <EmptyState title="暂无数据" hint={emptyHint} />
  if (['source_unavailable', 'upstream_error', 'rate_limited', 'invalid_request'].includes(payload.state)) {
    return <ErrorState title="新闻数据暂不可用" hint={payload.message ?? undefined} retrying={query.isFetching} onRetry={() => void query.refetch()} />
  }
  if (payload.state === 'no_data') return <EmptyState title="暂无数据" hint={payload.message ?? emptyHint} />
  return <>{children(payload)}</>
}

function ListItem({ title, meta, children, href, action }: {
  title: string; meta?: ReactNode; children?: ReactNode; href?: string; action?: ReactNode
}) {
  return (
    <li className="rounded-lg border border-border bg-elevated/40 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium leading-5 text-foreground">{title}</div>
          {meta && <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">{meta}</div>}
          {children && <div className="mt-2 whitespace-pre-wrap text-xs leading-5 text-secondary">{children}</div>}
        </div>
        {(action || href) && (
          <div className="flex shrink-0 items-center gap-2">
            {action}
            {href && <a href={href} target="_blank" rel="noopener noreferrer" title="打开原文" className="text-muted hover:text-accent"><ExternalLink className="h-3.5 w-3.5" /></a>}
          </div>
        )}
      </div>
    </li>
  )
}

function ArticlePanel({ seq, onSeqChange }: { seq: string; onSeqChange: (value: string) => void }) {
  const [clientType, setClientType] = useState('WEB')
  const [draft, setDraft] = useState(seq)
  const query = useQuery({
    queryKey: QK.newsArticle(clientType, seq),
    queryFn: () => api.newsArticleDetail(clientType, seq),
    enabled: !!seq.trim(),
  })
  const [eventQuery, setEventQuery] = useState('')
  const [searchParam, setSearchParam] = useState('event_name')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const events = useQuery({
    queryKey: QK.newsEvents({ query: eventQuery, search_param: searchParam, standard_start_time: start, standard_end_time: end, size: 20 }),
    queryFn: () => api.newsEvents({
      query: eventQuery || undefined, search_param: searchParam,
      standard_start_time: start || undefined, standard_end_time: end || undefined, size: 20,
    }),
  })

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">资讯详情</h2>
        <div className="flex flex-wrap items-center gap-2">
          <select value={clientType} onChange={e => setClientType(e.target.value)} className={inputCls} aria-label="客户端类型">
            {['WEB', 'WEB_ENCRYPTED', 'PC', 'PC_LIST'].map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="密文资讯标识 encoded_seq" className={cn(inputCls, 'min-w-0 flex-1')} />
          <button type="button" onClick={() => onSeqChange(draft.trim())} className={btnCls}><Search className="h-3.5 w-3.5" />查询</button>
        </div>
        <div className="mt-3">
          <NewsResult query={query} emptyHint="输入密文资讯标识后查询详情">
            {(payload) => {
              const article = payload.data
              if (!article) return null
              return (
                <article className="space-y-1.5">
                  <h3 className="text-sm font-medium text-foreground">{article.title}</h3>
                  <div className="text-[11px] text-muted">
                    {[article.source, article.author, article.publish_time_ms != null ? fmtMs(article.publish_time_ms) : null].filter(Boolean).join(' · ') || '来源未知'}
                  </div>
                  {article.summary && <p className="text-xs text-secondary">{article.summary}</p>}
                  <p className="whitespace-pre-wrap text-xs leading-6 text-secondary">{htmlText(article.content) || '暂无正文'}</p>
                </article>
              )
            }}
          </NewsResult>
        </div>
      </section>

      <section className="rounded-lg border border-border p-3">
        <h2 className="mb-2 text-xs font-medium text-foreground">资讯事件库</h2>
        <p className="mb-2 text-[11px] text-warning">实测该接口为同花顺 AI 客户端专用，外部 API Key 无法访问；下方展示上游降级提示。</p>
        <div className="flex flex-wrap items-center gap-2">
          <input value={eventQuery} onChange={e => setEventQuery(e.target.value)} placeholder="标题/正文关键词" className={cn(inputCls, 'min-w-0 flex-1')} />
          <select value={searchParam} onChange={e => setSearchParam(e.target.value)} className={inputCls} aria-label="检索域">
            <option value="event_name">标题</option><option value="event_summary">正文摘要</option>
          </select>
          <input type="date" value={start} onChange={e => setStart(e.target.value)} className={inputCls} aria-label="开始日期" />
          <input type="date" value={end} onChange={e => setEnd(e.target.value)} className={inputCls} aria-label="结束日期" />
        </div>
        <div className="mt-3">
          <NewsResult query={events} emptyHint="试试其他关键词或放宽日期范围">
            {(payload) => (
              <>
                <div className="mb-2 text-[11px] text-muted">命中 {payload.data?.total ?? 0} 条（本页最多 20 条）</div>
                <ul className="space-y-2">
                  {(payload.data?.items ?? []).map((raw, i) => {
                    const row = raw as {
                      event_id?: string; event_name?: string; event_summary?: string; standard_time?: string
                      event_grading?: { score?: string }; url?: string; related_stock?: { name?: string }[]
                    }
                    return (
                      <ListItem key={row.event_id ?? i} title={row.event_name ?? '未命名事件'}
                        meta={[row.standard_time, row.event_grading?.score && `评级 ${row.event_grading.score}`,
                          row.related_stock?.length ? `关联 ${row.related_stock.length} 只` : null].filter(Boolean).join(' · ')}
                        href={safeHref(row.url)}>
                        {row.event_summary}
                      </ListItem>
                    )
                  })}
                </ul>
              </>
            )}
          </NewsResult>
        </div>
      </section>
    </div>
  )
}

function FlashPanel({ onOpenArticle }: { onOpenArticle: (encodedSeq: string) => void }) {
  const qc = useQueryClient()
  const [tagId, setTagId] = useState('21103')
  const [seq, setSeq] = useState('0')
  const tag = Number(tagId) || 21103
  const query = useQuery({
    queryKey: QK.newsFlash(tag, seq),
    queryFn: () => api.newsFlashList(tag, seq),
    refetchInterval: seq === '0' ? 60_000 : false,
    placeholderData: (prev) => prev,
  })
  const refresh = useMutation({
    mutationFn: () => {
      const items = query.data?.data?.items ?? []
      const latest = items[0]
      if (!latest?.seq || latest.create_time_ms == null) throw new Error('当前列表没有可刷新的 seq 与时间')
      return api.newsFlashRefresh(tag, latest.seq, latest.create_time_ms)
    },
    onSuccess: (payload) => {
      if (payload.state !== 'ok' || !payload.data?.items.length) {
        toast(payload.message ?? '暂无新快讯')
        return
      }
      qc.setQueryData<NewsPayload<{ items: NewsFlashItem[] }>>(QK.newsFlash(tag, seq), old => {
        const seen = new Set<string>()
        const items = [...payload.data!.items, ...(old?.data?.items ?? [])].filter(item => {
          const key = item.id ?? item.seq ?? ''
          if (!key || seen.has(key)) return false
          seen.add(key)
          return true
        })
        return { state: 'ok', kind: 'flash_list', message: null, cached: false, cache_ttl_seconds: 0, data: { items } }
      })
      toast(`新增 ${payload.data.items.length} 条快讯`, 'success')
    },
    onError: (err: Error) => toast(err.message, 'error'),
  })

  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted">栏目 ID</span>
        <input value={tagId} onChange={e => setTagId(e.target.value)} className={cn(inputCls, 'w-24')} aria-label="快讯栏目 ID" />
        <button type="button" className={btnCls} disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          <RefreshCw className={cn('h-3.5 w-3.5', refresh.isPending && 'animate-spin')} />刷新
        </button>
        <button type="button" className={btnCls} onClick={() => setSeq('0')}>回到首屏</button>
        {seq !== '0' && <span className="text-[11px] text-muted">当前游标 {seq}</span>}
      </div>
      <div className="mt-3">
        <NewsResult query={query} emptyHint="该栏目暂无快讯；可尝试其他栏目 ID">
          {(payload) => {
            const items = payload.data?.items ?? []
            return (
              <>
                <ul className="space-y-2">
                  {items.map((item, i) => {
                    const detailSeq = encodedSeqFromUrl(item.encoded_url)
                    return (
                      <ListItem key={item.id ?? item.seq ?? i} title={item.title || '快讯'}
                        meta={[fmtMs(item.create_time_ms), ...(item.tags ?? []).map(t => t.name).filter(Boolean)].filter(Boolean).join(' · ')}
                        href={safeHref(item.encoded_url)}
                        action={detailSeq && (
                          <button type="button" className="text-[11px] text-accent hover:underline"
                            onClick={() => onOpenArticle(detailSeq)}>详情</button>
                        )}>
                        {item.summary}
                      </ListItem>
                    )
                  })}
                </ul>
                {items.length > 0 && items[items.length - 1].seq && (
                  <div className="mt-3 flex justify-center">
                    <button type="button" className={btnCls} onClick={() => setSeq(items[items.length - 1].seq ?? '0')}>加载更早</button>
                  </div>
                )}
              </>
            )
          }}
        </NewsResult>
      </div>
    </section>
  )
}

function NoticePanel() {
  const [draft, setDraft] = useState('')
  const [seq, setSeq] = useState('')
  const query = useQuery({
    queryKey: QK.newsNotice(seq),
    queryFn: () => api.newsNoticeDetail(seq),
    enabled: !!seq.trim(),
  })
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap gap-2">
        <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="公告序号 seq" className={cn(inputCls, 'min-w-0 flex-1')} />
        <button type="button" className={btnCls} onClick={() => setSeq(draft.trim())}><Search className="h-3.5 w-3.5" />查询</button>
      </div>
      <div className="mt-3">
        <NewsResult query={query} emptyHint="输入公告序号查询详情与结构化解读">
          {(payload) => {
            const notice = payload.data
            if (!notice) return null
            return (
              <div className="space-y-3">
                <div>
                  <h3 className="text-sm font-medium text-foreground">{notice.title}</h3>
                  <div className="mt-1 text-[11px] text-muted">{notice.report_period ?? '无报告期'} · {notice.seq}</div>
                </div>
                {notice.summary && (
                  <div className="rounded-md border border-border p-3">
                    <div className="mb-1 text-xs font-medium text-foreground">公告解读 · {notice.summary.notice_type ?? '—'}</div>
                    {notice.summary.items.map((item, i) => (
                      <div key={i} className="mt-2 text-xs text-secondary">
                        <div className="font-medium text-foreground">{item.title}</div>
                        <div className="whitespace-pre-wrap">{htmlText(item.content)}</div>
                      </div>
                    ))}
                  </div>
                )}
                {notice.monthly_report_interpretation && (
                  <div className="rounded-md border border-border p-3">
                    <div className="mb-1 text-xs font-medium text-foreground">月度解读 · {notice.monthly_report_interpretation.notice_type ?? '—'}</div>
                    {notice.monthly_report_interpretation.findings.map((finding, i) => (
                      <div key={i} className="mt-2 text-xs text-secondary">
                        <div className="font-medium text-foreground">{finding.title}</div>
                        {finding.blocks.map((block, j) => (
                          <p key={j} className="whitespace-pre-wrap">{block.sub_title ? `${block.sub_title}：` : ''}{htmlText(block.content_html)}</p>
                        ))}
                      </div>
                    ))}
                    {notice.monthly_report_interpretation.summary_html && (
                      <p className="mt-2 whitespace-pre-wrap text-xs text-secondary">{htmlText(notice.monthly_report_interpretation.summary_html)}</p>
                    )}
                  </div>
                )}
              </div>
            )
          }}
        </NewsResult>
      </div>
    </section>
  )
}

function TopicPanel() {
  const [tabId, setTabId] = useState('1')
  const [cursor, setCursor] = useState<string | undefined>(undefined)
  const [importantOnly, setImportantOnly] = useState(false)
  const tab = Number(tabId) || 1
  const query = useQuery({
    queryKey: QK.newsTopics(tab, cursor, 10, importantOnly),
    queryFn: () => api.newsTopicHot({ tab_id: tab, cursor, limit: 10, important_only: importantOnly }),
  })
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted">栏目 ID</span>
        <input value={tabId} onChange={e => setTabId(e.target.value)} className={cn(inputCls, 'w-24')} aria-label="专题栏目 ID" />
        <label className="flex items-center gap-1.5 text-[11px] text-secondary">
          <input type="checkbox" checked={importantOnly} onChange={e => setImportantOnly(e.target.checked)} />只看重要
        </label>
        <button type="button" className={btnCls} onClick={() => setCursor(undefined)}>重置</button>
      </div>
      <div className="mt-3">
        <NewsResult query={query} emptyHint="该栏目暂无专题内容">
          {(payload) => {
            const items = payload.data?.items ?? []
            return (
              <>
                <ul className="space-y-2">
                  {items.map((item: NewsTopicItem, i) => (
                    <ListItem key={item.id ?? i} title={item.title}
                      meta={[item.pin_label, item.type, item.source_created_at].filter(Boolean).join(' · ')}
                      href={safeHref(item.url)}>
                      {item.cover && <span className="text-muted">封面：{item.cover}</span>}
                    </ListItem>
                  ))}
                </ul>
                {payload.data?.has_more && payload.data.next_cursor && (
                  <div className="mt-3 flex justify-center">
                    <button type="button" className={btnCls} onClick={() => setCursor(payload.data?.next_cursor ?? undefined)}>下一页</button>
                  </div>
                )}
              </>
            )
          }}
        </NewsResult>
      </div>
    </section>
  )
}

function AnomalyPanel() {
  const [begin, setBegin] = useState(() => cnDate(-1))
  const [end, setEnd] = useState(() => cnDate(0))
  const [thscode, setThscode] = useState('')
  const query = useQuery({
    queryKey: QK.newsAnomaly(begin, end, thscode),
    queryFn: () => api.newsAnomalyAnalysis({ begin_date: begin, end_date: end, thscode: thscode.trim() || undefined }),
  })
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={begin} onChange={e => setBegin(e.target.value)} className={inputCls} aria-label="开始日期" />
        <span className="text-[11px] text-muted">至</span>
        <input type="date" value={end} onChange={e => setEnd(e.target.value)} className={inputCls} aria-label="结束日期" />
        <input value={thscode} onChange={e => setThscode(e.target.value)} placeholder="标的 thscode（可空=全量）" className={cn(inputCls, 'min-w-0 flex-1')} />
      </div>
      <p className="mt-2 text-[11px] text-muted">全量查询按天聚合（最多 30 天）；指定标的可查更长范围。上游偶发降级时只提示，不把空结果当作无异动。</p>
      <div className="mt-3">
        <NewsResult query={query} emptyHint="调整日期范围后再试">
          {(payload) => {
            const items = payload.data?.items ?? []
            return (
              <>
                {payload.data?.was_truncated && (
                  <div className="mb-2 flex items-center gap-1.5 text-[11px] text-warning">
                    <AlertTriangle className="h-3.5 w-3.5" />上游已按上限截断日期范围
                  </div>
                )}
                {payload.data?.partial && (
                  <div className="mb-2 flex items-center gap-1.5 text-[11px] text-warning">
                    <AlertTriangle className="h-3.5 w-3.5" />其中 {payload.data.skipped_days ?? 0} 天上游不可用，当前为部分日期结果
                  </div>
                )}
                <ul className="space-y-2">
                  {items.map((item: NewsAnomalyItem, i) => (
                    <ListItem key={item.analysis_id ?? i} title={item.tag_name ?? item.anomaly_type ?? '异动解读'}
                      meta={[item.domain, item.analysis_id, item.create_time != null ? `时间原值 ${item.create_time}` : null].filter(Boolean).join(' · ')}
                      href={safeHref(item.link_url)}>
                      {htmlText(item.analysis_content) || (item.key_word_list ?? []).join('、')}
                    </ListItem>
                  ))}
                </ul>
              </>
            )
          }}
        </NewsResult>
      </div>
    </section>
  )
}

function TimelinePanel() {
  const [draft, setDraft] = useState('300033.SZ')
  const [thscode, setThscode] = useState('300033.SZ')
  const [offset, setOffset] = useState('first')
  const query = useQuery({
    queryKey: QK.newsTimeline(thscode, offset, 20),
    queryFn: () => api.newsStockTimeline({ thscode, offset, size: 20 }),
  })
  return (
    <section className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="完整同花顺代码，如 300033.SZ" className={cn(inputCls, 'min-w-0 flex-1')} />
        <button type="button" className={btnCls} onClick={() => { setThscode(draft.trim()); setOffset('first') }}><Search className="h-3.5 w-3.5" />查询</button>
      </div>
      <div className="mt-3">
        <NewsResult query={query} emptyHint="该标的暂无分时新闻">
          {(payload) => {
            const items = payload.data?.items ?? []
            return (
              <>
                <ul className="space-y-2">
                  {items.map((item: NewsTimelineItem, i) => (
                    <ListItem key={item.id ?? i} title={item.title}
                      meta={[fmtMs(item.publish_time_ms), item.source, item.author].filter(Boolean).join(' · ')}
                      href={safeHref(item.jump_url)}>
                      {item.summary}
                    </ListItem>
                  ))}
                </ul>
                {payload.data?.has_more && payload.data.offset && (
                  <div className="mt-3 flex justify-center">
                    <button type="button" className={btnCls} onClick={() => setOffset(payload.data?.offset ?? 'first')}>下一页</button>
                  </div>
                )}
              </>
            )
          }}
        </NewsResult>
      </div>
    </section>
  )
}

export function NewsPage() {
  const [tab, setTab] = useState<Tab>('flash')
  const [articleSeq, setArticleSeq] = useState('')
  const status = useQuery({ queryKey: QK.newsStatus, queryFn: () => api.newsStatus(), staleTime: 60_000 })
  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="新闻"
        subtitle="资讯 · 快讯 · 公告 · 专题 · 异动解读 · 个股分时（扶摇数据源，缓存 7 天）"
        titleExtra={status.data?.available ? (
          <span className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-secondary">已接入</span>
        ) : (
          <span className="rounded-full bg-danger/10 px-2 py-0.5 text-[10px] text-danger">未配置数据源</span>
        )}
      />
      <div className="flex flex-wrap gap-1 border-b border-border px-4 py-2">
        {TABS.map(item => (
          <button key={item.id} type="button" onClick={() => setTab(item.id)}
            className={cn('rounded-md px-3 py-1.5 text-xs transition-colors',
              tab === item.id ? 'bg-accent/10 text-accent' : 'text-muted hover:bg-elevated hover:text-foreground')}>
            {item.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-5xl">
          {tab === 'article' && <ArticlePanel seq={articleSeq} onSeqChange={setArticleSeq} />}
          {tab === 'flash' && <FlashPanel onOpenArticle={(value) => { setArticleSeq(value); setTab('article') }} />}
          {tab === 'notice' && <NoticePanel />}
          {tab === 'topic' && <TopicPanel />}
          {tab === 'anomaly' && <AnomalyPanel />}
          {tab === 'timeline' && <TimelinePanel />}
        </div>
      </div>
    </div>
  )
}
