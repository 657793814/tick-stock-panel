/**
 * 关于面板 — 关于我(开发者与联系方式)、版权 两个区块。
 *
 * 「关于我」整合自 local-client 的关于页(开发者卡 + 项目地址 + 联系方式),
 * 放在版权上方; 原「版本」「打赏」区块已下线。
 */
import { useState } from 'react'
import { ScrollText, ExternalLink, Scale, User, Copy, Check, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { OssLicenseDialog, LicenseComplianceNotice } from '@/components/OssLicenseDialog'
import { PROJECT_LICENSE_NAME, PROJECT_REPO_URL } from '@/data/ossLicenses'

/** 开发者信息 — 集中一处, 改这里即可, 不必动结构。 */
const DEVELOPER = {
  name: '咚咚锵',
  title: 'tick-stock-panel · 开发者',
  bio: '做这款工具，是为了把行情数据、复盘与因子研究整合成一台电脑上就能用的投资工作台——本地优先，数据只留在自己手里。',
}

/** 开发者卡的四条事实, 写法保守, 避免和实际版本对不上。 */
const FACTS = [
  { label: '运行方式', value: '本地离线' },
  { label: '数据', value: '只存在本机' },
  { label: '更新', value: 'GitHub 下载' },
  { label: '协议', value: 'MIT 开源' },
]

const CONTACT_QR = {
  src: '/contact-wechat.jpg',
  alt: '微信二维码：咚咚锵',
  caption: '微信扫一扫 · 添加好友交流',
}

export function SettingsAboutPanel() {
  const [licenseOpen, setLicenseOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [qrZoom, setQrZoom] = useState<{ src: string; alt: string; caption: string } | null>(null)

  const copyProject = async () => {
    try {
      await navigator.clipboard.writeText(PROJECT_REPO_URL)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="pb-8 flex flex-col gap-6">
      {/* ===== 关于我 (整合自 local-client 关于页) ===== */}
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2 mb-4">
          <User className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">关于我</h3>
          <span className="text-[11px] text-muted">开发者与联系方式</span>
        </div>

        {/* 开发者 */}
        <div className="flex items-start gap-3">
          <span
            className="h-10 w-10 shrink-0 rounded-full bg-accent/10 text-accent text-base font-semibold
                       flex items-center justify-center select-none"
            aria-hidden="true"
          >
            {DEVELOPER.name.slice(0, 1)}
          </span>
          <div className="min-w-0">
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-sm font-medium text-foreground">{DEVELOPER.name}</span>
              <span className="text-[11px] text-muted">{DEVELOPER.title}</span>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-secondary">{DEVELOPER.bio}</p>
          </div>
        </div>

        {/* 项目地址 */}
        <div className="mt-4 pt-3 border-t border-border/60 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm text-foreground">项目地址</div>
            <div className="text-[11px] text-muted truncate">源码与版本记录</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <a
              href={PROJECT_REPO_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                         bg-elevated text-secondary hover:text-foreground transition-colors min-w-0"
            >
              <ExternalLink className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{PROJECT_REPO_URL.replace(/^https?:\/\//, '')}</span>
            </a>
            <button
              onClick={copyProject}
              title="复制链接"
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-btn text-xs
                         bg-elevated text-secondary hover:text-foreground transition-colors"
            >
              {copied ? <Check className="h-3.5 w-3.5 text-bull" /> : <Copy className="h-3.5 w-3.5" />}
              {copied ? '已复制' : '复制'}
            </button>
          </div>
        </div>

        {/* 事实条 */}
        <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-2">
          {FACTS.map(({ label, value }) => (
            <div key={label} className="rounded-btn border border-border/60 bg-elevated/60 px-3 py-2">
              <div className="text-[10px] text-muted">{label}</div>
              <div className="mt-0.5 text-xs font-medium text-foreground">{value}</div>
            </div>
          ))}
        </div>

        {/* 微信联系 */}
        <div className="mt-4 pt-3 border-t border-border/60 flex items-center justify-between gap-6">
          <div className="min-w-0 flex-1">
            <div className="text-sm text-foreground">微信联系</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
              扫码添加微信好友，交流使用问题与功能建议。点击右侧二维码可放大。
            </div>
            <div className="mt-1 text-[10px] text-muted/70">交流范围：使用问题 · 功能建议 · 数据源适配</div>
          </div>
          <button
            onClick={() => setQrZoom({ src: CONTACT_QR.src, alt: CONTACT_QR.alt, caption: CONTACT_QR.caption })}
            title="点击放大"
            className="group shrink-0 rounded-lg border border-border bg-white p-2
                       hover:border-accent/40 transition-colors"
          >
            <img src={CONTACT_QR.src} alt={CONTACT_QR.alt} className="h-24 w-24 object-contain" />
            <div className="mt-1 text-center text-[10px] text-neutral-500 group-hover:text-neutral-700">
              微信二维码
            </div>
          </button>
        </div>

        <p className="mt-3 text-[10px] leading-relaxed text-muted">
          本面板不会上传你的自选与操作记录; 只有在你主动发起拉取时, 数据才会发给你自己配置的数据源; 应用不收集使用日志。
        </p>
      </section>

      {/* ===== 版权 ===== */}
      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div className="flex items-center gap-2">
            <Scale className="h-4 w-4 text-accent" />
            <h3 className="text-sm font-medium text-foreground">版权</h3>
          </div>
          <span className="shrink-0 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
            {PROJECT_LICENSE_NAME}
          </span>
        </div>

        {/* 版权署名 — 界面常驻, 避免仅存在于 LICENSE 文件时被整包删除 */}
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">© 2026 tick-stock-panel contributors</div>
            <div className="text-[11px] text-muted truncate">基于 MIT 协议开源 · 遵照协议分发时须保留版权与许可声明</div>
          </div>
          <a
            href={PROJECT_REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex shrink-0 items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                       bg-elevated text-secondary hover:text-foreground transition-colors"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            仓库
          </a>
        </div>

        {/* 开源许可 — 点击弹窗查看完整内容 */}
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="min-w-0">
            <div className="text-sm text-foreground">开源许可</div>
            <div className="text-[11px] text-muted truncate">本项目协议全文与第三方组件许可清单</div>
          </div>
          <button
            onClick={() => setLicenseOpen(true)}
            className="inline-flex shrink-0 items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs
                       bg-elevated text-secondary hover:text-foreground transition-colors"
          >
            <ScrollText className="h-3.5 w-3.5" />
            查看许可
          </button>
        </div>

        <div className="mt-3 border-t border-border/60 pt-3 text-[11px] leading-relaxed text-secondary">
          <p>
            图表组件：
            <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-foreground">
              TradingView Lightweight Charts™
            </a>
          </p>
          <p className="mt-1">Copyright (с) 2025 TradingView, Inc.</p>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
            <a href="/third-party/lightweight-charts/LICENSE" target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-foreground">Apache-2.0 许可</a>
            <a href="/third-party/lightweight-charts/NOTICE" target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-foreground">NOTICE</a>
          </div>
        </div>

        {/* 许可合规声明 — 醒目警示 */}
        <LicenseComplianceNotice compact />
      </section>

      {licenseOpen && <OssLicenseDialog onClose={() => setLicenseOpen(false)} />}

      {/* 二维码大图 — 桌面端无法直接扫码时方便手机扫 */}
      {qrZoom && (
        <Modal
          onClose={() => setQrZoom(null)}
          ariaLabel={qrZoom.alt}
          panelClassName="w-auto bg-white rounded-card shadow-xl p-4"
        >
          <div className="flex flex-col items-center">
            <img
              src={qrZoom.src}
              alt={qrZoom.alt}
              className="max-w-[70vw] max-h-[70vh] object-contain"
            />
            <div className="mt-2 flex w-full items-center justify-between gap-4">
              <span className="text-xs text-neutral-600">{qrZoom.caption}</span>
              <button
                onClick={() => setQrZoom(null)}
                className="p-1 rounded hover:bg-neutral-100 transition-colors text-neutral-500"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
