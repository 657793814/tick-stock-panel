import { lazy } from 'react'
import { Newspaper } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const NewsPage = lazy(() => import('./Page').then(m => ({ default: m.NewsPage })))

const extension: FrontendExtension = {
  id: 'official.news',
  apiVersion: 1,
  routes: [
    { id: 'news', path: '/news', component: NewsPage },
  ],
  navigation: [
    // 0.5 = 默认基准中看板(0)与自选(1)之间
    { id: 'news', routeId: 'news', label: '新闻', icon: Newspaper, order: 0.5 },
  ],
}

export default extension
