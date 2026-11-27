import { lazy } from 'react'
import { Landmark } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const EastmoneyPage = lazy(() => import('./Page').then(m => ({ default: m.EastmoneyPage })))

const extension: FrontendExtension = {
  id: 'official.eastmoney',
  apiVersion: 1,
  routes: [
    { id: 'eastmoney', path: '/eastmoney', component: EastmoneyPage },
  ],
  navigation: [
    // 0.7 = 新闻(0.5) / XTick(0.6) 之后, 自选(1) 之前
    { id: 'eastmoney', routeId: 'eastmoney', label: '东方财富', icon: Landmark, order: 0.7 },
  ],
}

export default extension
