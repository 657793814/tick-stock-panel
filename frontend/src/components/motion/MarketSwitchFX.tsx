/**
 * 市场切换过场 — 扫描线 + 扩散环 + 解码标签。
 *
 * 纯 CSS 动画、pointer-events:none，进场即自动淡出，不参与布局也不阻塞点击。
 * 与 Layout 中按 market 重挂载的 key 配合：切换瞬间盖一层「重新接入」的视觉，
 * 让骨架屏出现变得可预期，而不是「页面闪了一下」。
 */
import { DecodeText } from './DecodeText'
import { marketLabel, type Market } from '@/lib/market'

export function MarketSwitchFX({ market, nonce }: { market: Market; nonce: number }) {
  if (!nonce) return null
  return (
    <div key={nonce} className={`tf-switchfx tf-switchfx-${market}`} aria-hidden="true">
      <span className="tf-switchfx-scan" />
      <span className="tf-switchfx-grid" />
      <span className="tf-switchfx-ring" />
      <span className="tf-switchfx-ring tf-switchfx-ring-alt" />
      <div className="tf-switchfx-hud">
        <span className="tf-switchfx-dot" />
        <DecodeText
          text={`LINK → ${marketLabel(market)}`}
          className="tf-switchfx-text"
          duration={460}
          trigger={nonce}
        />
      </div>
    </div>
  )
}
