import { lazy } from 'react'
import { Activity } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const XTickPage = lazy(() => import('./Page').then(m => ({ default: m.XTickPage })))

const extension: FrontendExtension = {
  id: 'official.xtick',
  apiVersion: 1,
  routes: [
    { id: 'xtick', path: '/xtick', component: XTickPage },
  ],
  navigation: [
    // 0.6 = 看板(0) → 新闻(0.5) 之后, 自选(1) 之前
    { id: 'xtick', routeId: 'xtick', label: 'XTick', icon: Activity, order: 0.6 },
  ],
}

export default extension
