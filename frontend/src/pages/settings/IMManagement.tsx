/**
 * IM 管理 — 独立设置分区: 集中管理所有 IM 通知推送渠道。
 *
 * 覆盖渠道: 飞书群 Webhook、企业微信群 Webhook、第三方 JSON Webhook、
 * SMTP 邮件、企业微信智能机器人(长连接)。
 * 渠道配置后即可作为监控告警的外部推送渠道; 勾选=全局打开该渠道,
 * 取消勾选=全局关闭。与「实时监控 → 推送通知」共用同一组偏好(同一后端),
 * 本分区作为统一入口维护 IM 相关配置。
 */
import { useState, useCallback, useEffect, type ComponentType } from 'react'
import { useQueryClient, useMutation } from '@tanstack/react-query'
import { Webhook, Mail, MessageCircle, ChevronDown, Bot, RefreshCw } from 'lucide-react'
import { usePreferences } from '@/lib/useSharedQueries'
import { api, type EmailSmtpConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const EMPTY_EMAIL_SMTP: EmailSmtpConfig = {
  host: '',
  port: 465,
  security: 'ssl',
  username: '',
  from_address: '',
  to_addresses: [],
}

// ===== 本分区自包含的通用组件 =====

interface CardProps {
  icon: ComponentType<{ className?: string }>
  title: string
  children: React.ReactNode
}

function Card({ icon: Icon, title, children }: CardProps) {
  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
        </div>
      </div>
      {children}
    </section>
  )
}

function TestSendButton({ test, configured }: {
  test: { isPending: boolean; mutate: () => void }
  configured: boolean
}) {
  return (
    <button
      onClick={() => test.mutate()}
      disabled={test.isPending || !configured}
      title={!configured ? '请先完成该渠道配置' : '发送测试消息'}
      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:text-foreground text-xs disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
    >
      {test.isPending ? '测试中…' : '测试'}
    </button>
  )
}

function TestResult({ test }: {
  test: { data?: { ok: boolean; detail: string } | null; isError: boolean; error?: Error | null; reset: () => void }
}) {
  // 成功结果 2 秒后自动消失; 失败保留, 便于阅读
  useEffect(() => {
    if (test.data?.ok) {
      const t = window.setTimeout(test.reset, 2000)
      return () => window.clearTimeout(t)
    }
  }, [test.data, test.reset])

  let text: string | null = null
  let tone = ''
  if (test.isError) {
    text = String(test.error?.message ?? '发送失败')
    tone = 'text-danger'
  } else if (test.data) {
    text = (test.data.ok ? '✓ ' : '✗ ') + test.data.detail
    tone = test.data.ok ? 'text-emerald-500' : 'text-danger'
  }
  if (!text) return null
  return <span className={`min-w-0 text-[11px] leading-snug ${tone}`}>{text}</span>
}

// ===== IM 管理面板 =====

export function SettingsIMManagementPanel() {
  const qc = useQueryClient()
  const { data: prefs } = usePreferences()
  const enabledChannels = prefs?.webhook_enabled_channels ?? prefs?.webhook_default_channels ?? []
  // 同一时刻只展开一个渠道配置区 (点击当前行可收起)
  const [expandedChannel, setExpandedChannel] = useState<string | null>('feishu')

  // 飞书群 Webhook
  const feishuWebhookUrl = prefs?.feishu_webhook_url ?? ''
  const feishuWebhookSecret = prefs?.feishu_webhook_secret ?? ''
  const [feishuDraft, setFeishuDraft] = useState(feishuWebhookUrl)
  const [feishuSecretDraft, setFeishuSecretDraft] = useState(feishuWebhookSecret)
  const [feishuError, setFeishuError] = useState('')
  useEffect(() => {
    setFeishuDraft(feishuWebhookUrl)
    setFeishuSecretDraft(feishuWebhookSecret)
  }, [feishuWebhookUrl, feishuWebhookSecret])

  const saveFeishuWebhook = useMutation({
    mutationFn: ({ url, secret }: { url: string; secret: string }) => api.updateFeishuWebhook(url, secret),
    onSuccess: () => {
      setFeishuError('')
      toast('飞书 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setFeishuError(String(err?.message ?? '保存失败')),
  })
  const FEISHU_PREFIX = 'https://open.feishu.cn/open-apis/bot/v2/hook/'
  const submitFeishu = useCallback(() => {
    const url = feishuDraft.trim()
    const secret = feishuSecretDraft.trim()
    if (url && !url.startsWith(FEISHU_PREFIX)) {
      setFeishuError('地址需以 ' + FEISHU_PREFIX + ' 开头')
      return
    }
    saveFeishuWebhook.mutate({ url, secret })
  }, [feishuDraft, feishuSecretDraft, saveFeishuWebhook])
  const testFeishu = useMutation({ mutationFn: () => api.sendTestWebhook('feishu') })

  // 飞书智能机器人 (自建应用: App ID + App Secret + 目标群 chat_id, 走 im/v1/messages)
  const feishuAppId = prefs?.feishu_app_id ?? ''
  const feishuAppSecretSet = prefs?.feishu_app_secret_set ?? false
  const feishuAppChatId = prefs?.feishu_app_chat_id ?? ''
  const [appIdDraft, setAppIdDraft] = useState(feishuAppId)
  const [appSecretDraft, setAppSecretDraft] = useState('')
  const [appChatDraft, setAppChatDraft] = useState(feishuAppChatId)
  const [appError, setAppError] = useState('')
  const [appChats, setAppChats] = useState<{ chat_id: string; name: string }[]>([])
  const feishuAppConfigured = !!(feishuAppId && feishuAppSecretSet && feishuAppChatId)
  useEffect(() => {
    setAppIdDraft(feishuAppId)
    setAppChatDraft(feishuAppChatId)
    setAppSecretDraft('')
  }, [feishuAppId, feishuAppChatId, feishuAppSecretSet])

  const saveFeishuApp = useMutation({
    mutationFn: ({ appId, chatId, secret }: { appId: string; chatId: string; secret?: string }) =>
      api.updateFeishuApp(appId, chatId, secret),
    onSuccess: () => {
      setAppError('')
      setAppSecretDraft('')
      toast('飞书智能机器人配置已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setAppError(String(err?.message ?? '保存失败')),
  })
  const submitFeishuApp = useCallback(() => {
    saveFeishuApp.mutate({
      appId: appIdDraft.trim(),
      chatId: appChatDraft.trim(),
      ...(appSecretDraft.trim() ? { secret: appSecretDraft.trim() } : {}),
    })
  }, [appIdDraft, appChatDraft, appSecretDraft, saveFeishuApp])
  // 读取机器人已加入的群, 免去手工找 chat_id
  const loadAppChats = useMutation({
    mutationFn: () => api.listFeishuAppChats(),
    onSuccess: (data) => {
      if (data.ok) {
        setAppChats(data.chats)
        setAppError(data.chats.length ? '' : '该应用机器人还没有加入任何群, 请先把机器人拉进群')
      } else {
        setAppError(data.detail || '读取群列表失败')
      }
    },
    onError: (err: any) => setAppError(String(err?.message ?? '读取群列表失败')),
  })
  const testFeishuApp = useMutation({ mutationFn: () => api.sendTestWebhook('feishu_app') })

  // 企业微信群 Webhook (Webhook 地址 或 Key)
  const wecomWebhookUrl = prefs?.wecom_webhook_url ?? ''
  const [wecomDraft, setWecomDraft] = useState(wecomWebhookUrl)
  const [wecomError, setWecomError] = useState('')
  useEffect(() => {
    setWecomDraft(wecomWebhookUrl)
  }, [wecomWebhookUrl])
  const saveWecomWebhook = useMutation({
    mutationFn: (url: string) => api.updateWecomWebhook(url),
    onSuccess: () => {
      setWecomError('')
      toast('企业微信 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setWecomError(String(err?.message ?? '保存失败')),
  })
  const WECOM_PREFIX = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
  const submitWecom = useCallback(() => {
    const url = wecomDraft.trim()
    // 允许完整 URL 或纯 key (36位UUID样式)
    if (url && !url.startsWith(WECOM_PREFIX) && url.length < 20) {
      setWecomError('请输入完整 Webhook 地址或纯 key (至少 20 位)')
      return
    }
    saveWecomWebhook.mutate(url)
  }, [wecomDraft, saveWecomWebhook])
  const testWecom = useMutation({ mutationFn: () => api.sendTestWebhook('wecom') })

  // 第三方 JSON Webhook (HMAC-SHA256 可选签名)
  const customWebhookUrl = prefs?.custom_webhook_url ?? ''
  const customWebhookSecretSet = prefs?.custom_webhook_secret_set ?? false
  const [customDraft, setCustomDraft] = useState(customWebhookUrl)
  const [customSecretDraft, setCustomSecretDraft] = useState('')
  const [customError, setCustomError] = useState('')
  useEffect(() => {
    setCustomDraft(customWebhookUrl)
    setCustomSecretDraft('')
  }, [customWebhookUrl, customWebhookSecretSet])
  const saveCustomWebhook = useMutation({
    mutationFn: ({ url, secret }: { url: string; secret?: string }) => api.updateCustomWebhook(url, secret),
    onSuccess: () => {
      setCustomError('')
      setCustomSecretDraft('')
      toast('第三方 Webhook 已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setCustomError(String(err?.message ?? '保存失败')),
  })
  const submitCustom = useCallback(() => {
    const url = customDraft.trim()
    if (url && !/^https?:\/\//i.test(url)) {
      setCustomError('请输入完整的 HTTP(S) URL')
      return
    }
    saveCustomWebhook.mutate({
      url,
      ...(customSecretDraft ? { secret: customSecretDraft } : {}),
    })
  }, [customDraft, customSecretDraft, saveCustomWebhook])
  const testCustom = useMutation({ mutationFn: () => api.sendTestWebhook('custom') })

  // SMTP 邮件
  const emailSmtpConfig = prefs?.email_smtp_config ?? EMPTY_EMAIL_SMTP
  const emailSmtpPasswordSet = prefs?.email_smtp_password_set ?? false
  const emailConfigured = !!(
    emailSmtpConfig.host
    && emailSmtpConfig.from_address
    && emailSmtpConfig.to_addresses.length
    && (!emailSmtpConfig.username || emailSmtpPasswordSet)
  )
  const [emailDraft, setEmailDraft] = useState<EmailSmtpConfig>(emailSmtpConfig)
  const [emailPasswordDraft, setEmailPasswordDraft] = useState('')
  const [emailError, setEmailError] = useState('')
  useEffect(() => {
    setEmailDraft(emailSmtpConfig)
    setEmailPasswordDraft('')
  }, [emailSmtpConfig, emailSmtpPasswordSet])
  const saveEmailSmtp = useMutation({
    mutationFn: ({ config, password }: { config: EmailSmtpConfig; password?: string }) =>
      api.updateEmailSmtp(config, password),
    onSuccess: () => {
      setEmailError('')
      setEmailPasswordDraft('')
      toast('邮件推送配置已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setEmailError(String(err?.message ?? '保存失败')),
  })
  const submitEmail = useCallback(() => {
    saveEmailSmtp.mutate({
      config: {
        ...emailDraft,
        host: emailDraft.host.trim(),
        username: emailDraft.username.trim(),
        from_address: emailDraft.from_address.trim(),
        to_addresses: emailDraft.to_addresses.map(item => item.trim()).filter(Boolean),
      },
      ...(emailPasswordDraft ? { password: emailPasswordDraft } : {}),
    })
  }, [emailDraft, emailPasswordDraft, saveEmailSmtp])
  const testEmail = useMutation({ mutationFn: () => api.sendTestWebhook('email') })

  // 企业微信智能机器人 (BotID + Secret, 长连接通道)
  const wecomBotId = prefs?.wecom_bot_id ?? ''
  const wecomBotSecret = prefs?.wecom_bot_secret ?? ''
  const wecomBotEnabled = prefs?.wecom_bot_enabled ?? false
  const wecomBotChatId = prefs?.wecom_bot_chat_id ?? ''
  const wecomBotChats = prefs?.wecom_bot_chats ?? []
  const wecomBotConfigured = !!(wecomBotId && wecomBotSecret && wecomBotChatId)
  const [botIdDraft, setBotIdDraft] = useState(wecomBotId)
  const [botSecretDraft, setBotSecretDraft] = useState(wecomBotSecret)
  const [botChatDraft, setBotChatDraft] = useState(wecomBotChatId)
  const [botError, setBotError] = useState('')
  const [botStatus, setBotStatus] = useState<{ connected: boolean; last_error: string } | null>(null)
  useEffect(() => {
    setBotIdDraft(wecomBotId)
    setBotSecretDraft(wecomBotSecret)
  }, [wecomBotId, wecomBotSecret])
  useEffect(() => {
    setBotChatDraft(wecomBotChatId)
  }, [wecomBotChatId])

  // 智能机器人凭证保存 → 后端立即重建连接
  const saveWecomBot = useMutation({
    mutationFn: ({ botId, secret }: { botId: string; secret: string }) => api.updateWecomBot(botId, secret, true),
    onSuccess: (data) => {
      setBotError('')
      toast('智能机器人凭证已保存, 正在连接…', 'success')
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setBotError(String(err?.message ?? '保存失败')),
  })
  const submitBot = useCallback(() => {
    saveWecomBot.mutate({ botId: botIdDraft.trim(), secret: botSecretDraft.trim() })
  }, [botIdDraft, botSecretDraft, saveWecomBot])

  // 智能机器人长连接开关(不改动凭证): 开启→连接, 关闭→断开
  const toggleBotConnection = useMutation({
    mutationFn: (enabled: boolean) => api.toggleWecomBot(enabled),
    onSuccess: (data) => {
      setBotStatus({
        connected: data.wecom_bot_status?.connected ?? false,
        last_error: data.wecom_bot_status?.last_error ?? '',
      })
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
  })
  const saveBotChat = useMutation({
    mutationFn: () => api.updateWecomBotChat(botChatDraft.trim()),
    onSuccess: () => {
      setBotError('')
      toast('智能机器人目标会话已保存', 'success')
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    onError: (err: any) => setBotError(String(err?.message ?? '保存失败')),
  })
  const refreshBotChats = useMutation({
    mutationFn: () => api.listWecomBotChats(),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })
  const testWecomBot = useMutation({ mutationFn: () => api.sendTestWebhook('wecom_bot') })

  const toggleEnabledChannel = useCallback(
    (ch: string, enabled: boolean) => {
      const cur = prefs?.webhook_enabled_channels ?? prefs?.webhook_default_channels ?? []
      const next = enabled ? [...cur, ch] : cur.filter(c => c !== ch)
      api.updateWebhookEnabledChannels(next)
      qc.invalidateQueries({ queryKey: QK.preferences })
    },
    [prefs, qc],
  )

  const IM_CHANNELS: {
    key: string
    icon: ComponentType<{ className?: string }>
    title: string
    sub: string
    configured: boolean
    config?: React.ReactNode
    desc?: string
  }[] = [
    {
      key: 'feishu',
      icon: Webhook,
      title: '飞书',
      sub: '群推送 Webhook',
      configured: !!feishuWebhookUrl,
      config:
        <div className="border-t border-border/60 bg-base/30 p-3">
          <label className="block space-y-1.5">
            <span className="text-[11px] text-muted">Webhook 地址</span>
            <input
              value={feishuDraft}
              onChange={e => { setFeishuDraft(e.target.value); if (!testFeishu.isPending) testFeishu.reset() }}
              placeholder={FEISHU_PREFIX + 'xxxxxxxx'}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          <label className="block mt-2 space-y-1.5">
            <span className="text-[11px] text-muted">签名密钥 (可选 · 启用签名校验时填)</span>
            <input
              type="password"
              value={feishuSecretDraft}
              onChange={e => { setFeishuSecretDraft(e.target.value); if (!testFeishu.isPending) testFeishu.reset() }}
              placeholder="机器人未启用签名校验则留空"
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          {feishuError && <div className="mt-2 text-[11px] text-danger">{feishuError}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={submitFeishu}
              disabled={saveFeishuWebhook.isPending || (feishuDraft.trim() === feishuWebhookUrl && feishuSecretDraft.trim() === feishuWebhookSecret)}
              className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
            >
              {saveFeishuWebhook.isPending ? '保存中…' : '保存'}
            </button>
            <TestSendButton test={testFeishu} configured={!!feishuWebhookUrl} />
            {feishuWebhookUrl && <span className="text-[10px] text-emerald-500">● 已配置</span>}
            <TestResult test={testFeishu} />
          </div>
          <details className="mt-3 text-[10px] text-muted">
            <summary className="cursor-pointer hover:text-secondary">如何获取飞书 Webhook 地址?</summary>
            <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
              <li>打开飞书,进入目标群聊 → 群设置 → <b>群推送 Webhook</b></li>
              <li>点击「添加机器人」→ 选择「<b>自定义机器人</b>」</li>
              <li>填写机器人名称后添加,复制生成的 Webhook 地址</li>
              <li>安全设置若启用了「<b>签名校验</b>」,把密钥一并复制填到「签名密钥」框</li>
              <li>粘贴到上方输入框并保存</li>
            </ol>
            <p className="mt-1.5 pl-4 text-muted/70">
              📖 官方文档:
              <a href="https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                自定义机器人使用指南 ↗
              </a>
            </p>
          </details>
        </div>,
    },
    {
      key: 'feishu_app',
      icon: Bot,
      title: '飞书',
      sub: '智能机器人(应用)',
      configured: feishuAppConfigured,
      desc: '企业自建应用机器人: 用 App ID + App Secret 换 token 后按群推送, 可推送到机器人已加入的任意群。',
      config:
        <div className="border-t border-border/60 bg-base/30 p-3">
          <label className="block space-y-1.5">
            <span className="text-[11px] text-muted">App ID</span>
            <input
              value={appIdDraft}
              onChange={e => { setAppIdDraft(e.target.value); if (!testFeishuApp.isPending) testFeishuApp.reset() }}
              placeholder="cli_xxxxxxxxxxxxxxxx"
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          <label className="block mt-2 space-y-1.5">
            <span className="text-[11px] text-muted">App Secret</span>
            <input
              type="password"
              value={appSecretDraft}
              onChange={e => setAppSecretDraft(e.target.value)}
              placeholder={feishuAppSecretSet ? '已保存；留空保持不变' : '开发者后台「凭证与基础信息」里的 App Secret'}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          <label className="block mt-2 space-y-1.5">
            <span className="text-[11px] text-muted">目标群 chat_id</span>
            <div className="flex gap-2">
              <input
                value={appChatDraft}
                onChange={e => setAppChatDraft(e.target.value)}
                placeholder="oc_xxxxxxxxxxxxxxxx"
                className="h-9 min-w-0 flex-1 rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
              />
              <button
                type="button"
                onClick={() => loadAppChats.mutate()}
                disabled={loadAppChats.isPending || !appIdDraft.trim()}
                title="用已保存的 App ID/Secret 读取机器人所在的群"
                className="inline-flex shrink-0 items-center gap-1 rounded-btn bg-elevated px-2.5 text-[11px] text-secondary hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <RefreshCw className="h-3 w-3" />
                {loadAppChats.isPending ? '读取中…' : '读取群列表'}
              </button>
            </div>
          </label>
          {appChats.length > 0 && (
            <select
              value={appChatDraft}
              onChange={e => setAppChatDraft(e.target.value)}
              className="mt-2 h-9 w-full rounded-btn border border-border bg-base px-2 text-xs text-foreground"
            >
              <option value="">选择群…</option>
              {appChats.map(c => (
                <option key={c.chat_id} value={c.chat_id}>{c.name || c.chat_id}</option>
              ))}
            </select>
          )}
          {appError && <div className="mt-2 text-[11px] text-danger">{appError}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={submitFeishuApp}
              disabled={saveFeishuApp.isPending || (appIdDraft.trim() === feishuAppId && appChatDraft.trim() === feishuAppChatId && !appSecretDraft)}
              className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
            >
              {saveFeishuApp.isPending ? '保存中…' : '保存'}
            </button>
            <TestSendButton test={testFeishuApp} configured={feishuAppConfigured} />
            {feishuAppConfigured && <span className="text-[10px] text-emerald-500">● 已配置</span>}
            <TestResult test={testFeishuApp} />
          </div>
          <details className="mt-3 text-[10px] text-muted">
            <summary className="cursor-pointer hover:text-secondary">如何获取 App ID / Secret 与目标群?</summary>
            <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
              <li>打开飞书开放平台 → 开发者后台 → 创建/进入<b>企业自建应用</b></li>
              <li>「凭证与基础信息」页复制 <b>App ID</b> 和 <b>App Secret</b></li>
              <li>「权限管理」开通 <b>im:message:send_as_bot</b>(以应用身份发消息), 并发布版本</li>
              <li>把机器人加进目标群, 点上方「读取群列表」选择群; chat_id 也可从群设置或接口获取</li>
              <li>保存后点「测试」验证(失败会显示飞书返回的具体错误码)</li>
            </ol>
            <p className="mt-1.5 pl-4 text-muted/70">
              💡 与上面的「飞书 群推送 Webhook」互补: Webhook 固定一个群且地址即凭证; 应用机器人凭证更可控, 可换群。
            </p>
            <p className="mt-1.5 pl-4 text-muted/70">
              📖 官方文档:
              <a href="https://open.feishu.cn/document/server-docs/im-v1/message/create" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                发送消息 API ↗
              </a>
            </p>
          </details>
        </div>,
    },
    {
      key: 'wecom',
      icon: Webhook,
      title: '企业微信',
      sub: '群推送 Webhook',
      configured: !!wecomWebhookUrl,
      config:
        <div className="border-t border-border/60 bg-base/30 p-3">
          <label className="block space-y-1.5">
            <span className="text-[11px] text-muted">Webhook 地址 或 Key</span>
            <input
              value={wecomDraft}
              onChange={e => { setWecomDraft(e.target.value); if (!testWecom.isPending) testWecom.reset() }}
              placeholder={WECOM_PREFIX + '?key=xxxxxxxx' + ' 或直接填 key'}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          {wecomError && <div className="mt-2 text-[11px] text-danger">{wecomError}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={submitWecom}
              disabled={saveWecomWebhook.isPending || wecomDraft.trim() === wecomWebhookUrl}
              className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
            >
              {saveWecomWebhook.isPending ? '保存中…' : '保存'}
            </button>
            <TestSendButton test={testWecom} configured={!!wecomWebhookUrl} />
            {wecomWebhookUrl && <span className="text-[10px] text-emerald-500">● 已配置</span>}
            <TestResult test={testWecom} />
          </div>
          <details className="mt-3 text-[10px] text-muted">
            <summary className="cursor-pointer hover:text-secondary">如何获取企业微信 Webhook 地址?</summary>
            <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
              <li>打开企业微信,进入目标群聊 → 右上角「...」→ <b>群推送 Webhook</b></li>
              <li>点击「添加」→ 选择「<b>消息推送</b>」→ 填写名字</li>
              <li>复制生成的 <b>Webhook 地址</b>(含 key 参数),粘贴到上方输入框</li>
              <li>也可只复制 key 参数部分(= 后面的内容)填入</li>
              <li>企业微信群的消息可同步到绑定的个人微信,实现"微信推送"</li>
            </ol>
            <p className="mt-1.5 pl-4 text-muted/70">
              📖 官方文档:
              <a href="https://developer.work.weixin.qq.com/document/path/91770" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                群推送 Webhook 使用指南 ↗
              </a>
            </p>
          </details>
        </div>,
    },
    {
      key: 'custom',
      icon: Webhook,
      title: '第三方系统',
      sub: 'JSON Webhook',
      configured: !!customWebhookUrl,
      config:
        <div className="border-t border-border/60 bg-base/30 p-3">
          <label className="block space-y-1.5">
            <span className="text-[11px] text-muted">接收端 URL</span>
            <input
              value={customDraft}
              onChange={e => { setCustomDraft(e.target.value); if (!testCustom.isPending) testCustom.reset() }}
              placeholder="https://example.com/webhooks/tickflow"
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          <label className="block mt-2 space-y-1.5">
            <span className="text-[11px] text-muted">签名密钥 (可选 · HMAC-SHA256)</span>
            <input
              type="password"
              value={customSecretDraft}
              onChange={e => setCustomSecretDraft(e.target.value)}
              placeholder={customWebhookSecretSet ? '已保存；留空保持不变' : '留空则不签名'}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
            />
          </label>
          {customError && <div className="mt-2 text-[11px] text-danger">{customError}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={submitCustom}
              disabled={saveCustomWebhook.isPending || (customDraft.trim() === customWebhookUrl && !customSecretDraft)}
              className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
            >
              {saveCustomWebhook.isPending ? '保存中…' : '保存'}
            </button>
            <TestSendButton test={testCustom} configured={!!customWebhookUrl} />
            <TestResult test={testCustom} />
          </div>
          <details className="mt-3 text-[10px] text-muted">
            <summary className="cursor-pointer hover:text-secondary">请求格式</summary>
            <p className="mt-1.5 leading-relaxed">
              POST JSON 包含 event、timestamp、title、body、data。配置密钥后会附带
              X-TickFlow-Timestamp 和 X-TickFlow-Signature 请求头。
            </p>
          </details>
        </div>,
    },
    {
      key: 'email',
      icon: Mail,
      title: '邮件',
      sub: 'SMTP',
      configured: emailConfigured,
      desc: '邮件推送配置; 完整配置需 SMTP 主机、发件人、收件人, 且有认证用户名或已保存密码。',
      config:
        <div className="border-t border-border/60 bg-base/30 p-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_6rem_8rem]">
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">SMTP 主机</span>
              <input value={emailDraft.host} onChange={e => setEmailDraft(d => ({ ...d, host: e.target.value }))} placeholder="smtp.example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">端口</span>
              <input type="number" min={1} max={65535} value={emailDraft.port} onChange={e => setEmailDraft(d => ({ ...d, port: Number(e.target.value) }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">加密</span>
              <select value={emailDraft.security} onChange={e => setEmailDraft(d => ({ ...d, security: e.target.value as EmailSmtpConfig['security'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-2 text-xs text-foreground">
                <option value="ssl">SSL</option>
                <option value="starttls">STARTTLS</option>
                <option value="none">无</option>
              </select>
            </label>
          </div>
          <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">登录用户名 (可选)</span>
              <input value={emailDraft.username} onChange={e => setEmailDraft(d => ({ ...d, username: e.target.value }))} placeholder="bot@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">密码 / 授权码 (可选)</span>
              <input type="password" value={emailPasswordDraft} onChange={e => setEmailPasswordDraft(e.target.value)} placeholder={emailSmtpPasswordSet ? '已保存；留空保持不变' : '无认证服务器可留空'} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">发件人</span>
              <input value={emailDraft.from_address} onChange={e => setEmailDraft(d => ({ ...d, from_address: e.target.value }))} placeholder="bot@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">收件人 (多个用逗号分隔)</span>
              <input value={emailDraft.to_addresses.join(', ')} onChange={e => setEmailDraft(d => ({ ...d, to_addresses: e.target.value.split(/[,;，；\n]/) }))} placeholder="alerts@example.com" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground" />
            </label>
          </div>
          {emailError && <div className="mt-2 text-[11px] text-danger">{emailError}</div>}
          <div className="mt-2 flex items-center gap-2">
            <button onClick={submitEmail} disabled={saveEmailSmtp.isPending} className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors">
              {saveEmailSmtp.isPending ? '保存中…' : '保存'}
            </button>
            <TestSendButton test={testEmail} configured={emailConfigured} />
            <TestResult test={testEmail} />
          </div>
        </div>,
    },
  ]

  return (
    <Card icon={MessageCircle} title="IM 管理">
      <p className="text-xs text-secondary mb-4">
        管理所有 IM 通知推送渠道:
        <b className="text-foreground/80">飞书</b>、<b className="text-foreground/80">飞书智能机器人(应用)</b>、
        <b className="text-foreground/80">企业微信</b>、
        <b className="text-foreground/80">第三方系统</b>、<b className="text-foreground/80">邮件 (SMTP)</b>、
        <b className="text-foreground/80">企业微信智能机器人</b>。
        渠道配置后即可作为监控告警的外部推送渠道; 勾选=<b className="text-foreground/80">打开该推送渠道</b>,
        取消勾选则全局关闭。与「实时监控 → 推送通知」共用同一组偏好, 两处操作同步生效。
      </p>

      <div className="space-y-3">
        {IM_CHANNELS.map((ch) => {
          const expanded = expandedChannel === ch.key
          return (
            <div key={ch.key} className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
              <div
                onClick={() => setExpandedChannel(expanded ? null : ch.key)}
                className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
                title={expanded ? '收起配置' : '展开配置'}
              >
                <input
                  type="checkbox"
                  checked={enabledChannels.includes(ch.key)}
                  onChange={e => {
                    e.stopPropagation()
                    toggleEnabledChannel(ch.key, e.target.checked)
                  }}
                  onClick={e => e.stopPropagation()}
                  title="启用该推送渠道"
                  className="h-3 w-3 accent-accent cursor-pointer"
                />
                <span className="text-[11px] font-medium text-foreground">{ch.title}</span>
                <span className="text-[9px] text-muted">{ch.sub}</span>
                {enabledChannels.includes(ch.key) && (
                  <span className="rounded bg-accent/15 px-1 py-px text-[9px] text-accent">已启用</span>
                )}
                <span className={`ml-auto text-[9px] ${ch.configured ? 'text-emerald-500' : 'text-warning'}`}>
                  {ch.configured ? '已配置' : '未配置'}
                </span>
                <ChevronDown className={`h-3 w-3 text-muted transition-transform ${expanded ? 'rotate-180' : ''}`} />
              </div>
              {expanded && ch.config && (
                <div className="border-t border-border/60 bg-base/30 p-3">
                  {ch.desc && <p className="mb-2.5 text-[10px] text-muted">{ch.desc}</p>}
                  {ch.config}
                </div>
              )}
            </div>
          )
        })}

        {/* 企业微信智能机器人: 长连接开关 + 凭证 */}
        <div className="rounded-btn border border-border/60 bg-base/40 overflow-hidden">
          <div
            onClick={() => setExpandedChannel(expandedChannel === 'wecom_bot' ? null : 'wecom_bot')}
            className="flex items-center gap-2 px-2.5 py-2 cursor-pointer transition-colors hover:bg-base/60"
            title={expandedChannel === 'wecom_bot' ? '收起配置' : '展开配置'}
          >
            <input
              type="checkbox"
              checked={enabledChannels.includes('wecom_bot')}
              onChange={e => { e.stopPropagation(); toggleEnabledChannel('wecom_bot', e.target.checked) }}
              onClick={e => e.stopPropagation()}
              title="启用该推送渠道"
              className="h-3 w-3 accent-accent cursor-pointer"
            />
            <span className="text-[11px] font-medium text-foreground">企业微信</span>
            <span className="text-[9px] text-muted">智能机器人</span>
            {enabledChannels.includes('wecom_bot') && (
              <span className="rounded bg-accent/15 px-1 py-px text-[9px] text-accent">已启用</span>
            )}
            <span className={`ml-auto text-[9px] ${wecomBotConfigured ? 'text-emerald-500' : 'text-warning'}`}>
              {wecomBotConfigured ? (botStatus?.connected ? '已连接' : (wecomBotEnabled ? '连接中' : '已配置')) : '未配置'}
            </span>
            <ChevronDown className={`h-3 w-3 text-muted transition-transform ${expandedChannel === 'wecom_bot' ? 'rotate-180' : ''}`} />
          </div>
          {expandedChannel === 'wecom_bot' && (
            <div className="border-t border-border/60 bg-base/30 p-3">
              <p className="mb-2.5 text-[10px] text-muted leading-relaxed">
                左侧勾选是<b className="text-foreground/80">推送渠道开关</b>; 长连接开关在下方单独控制。
                主动推送需先在目标群里 @机器人发一条消息, 后端才能拿到该群的会话 ID。
              </p>
              <label className="block space-y-1.5">
                <span className="text-[11px] text-muted">BotID</span>
                <input
                  value={botIdDraft}
                  onChange={e => setBotIdDraft(e.target.value)}
                  placeholder="智能机器人的唯一标识"
                  className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                />
              </label>

              <label className="block mt-2 space-y-1.5">
                <span className="text-[11px] text-muted">Secret (长连接专用密钥)</span>
                <input
                  type="password"
                  value={botSecretDraft}
                  onChange={e => setBotSecretDraft(e.target.value)}
                  placeholder={wecomBotSecret ? '已保存；留空保持不变' : '开启长连接 API 模式后获取的密钥'}
                  className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                />
              </label>

              <label className="block mt-2 space-y-1.5">
                <span className="text-[11px] text-muted">目标会话 (推送到哪个群/人)</span>
                <div className="flex gap-2">
                  <input
                    value={botChatDraft}
                    onChange={e => setBotChatDraft(e.target.value)}
                    placeholder="在群里 @机器人后点「刷新会话」选择"
                    className="h-9 min-w-0 flex-1 rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground focus:outline-none focus:border-accent/50"
                  />
                  <button
                    type="button"
                    onClick={() => refreshBotChats.mutate()}
                    disabled={refreshBotChats.isPending}
                    className="inline-flex shrink-0 items-center gap-1 rounded-btn bg-elevated px-2.5 text-[11px] text-secondary hover:text-foreground disabled:opacity-40"
                  >
                    <RefreshCw className="h-3 w-3" />
                    {refreshBotChats.isPending ? '读取中…' : '刷新会话'}
                  </button>
                </div>
              </label>
              {wecomBotChats.length > 0 && (
                <select
                  value={botChatDraft}
                  onChange={e => setBotChatDraft(e.target.value)}
                  className="mt-2 h-9 w-full rounded-btn border border-border bg-base px-2 text-xs text-foreground"
                >
                  <option value="">选择会话…</option>
                  {wecomBotChats.map(c => (
                    <option key={c.chat_id} value={c.chat_id}>
                      {c.chat_id}（{c.chat_type || '未知'}{c.last_seen ? ` · ${c.last_seen}` : ''}）
                    </option>
                  ))}
                </select>
              )}

              {botError && <div className="mt-2 text-[11px] text-danger">{botError}</div>}
              {botStatus?.last_error && !botError && (
                <div className="mt-2 text-[11px] text-warning">连接异常: {botStatus.last_error}</div>
              )}

              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  onClick={submitBot}
                  disabled={saveWecomBot.isPending || (botIdDraft.trim() === wecomBotId && !botSecretDraft)}
                  className="px-3 py-1.5 rounded-btn bg-accent text-white text-xs font-medium disabled:opacity-50 cursor-pointer hover:bg-accent/90 transition-colors"
                >
                  {saveWecomBot.isPending ? '保存中…' : '保存凭证'}
                </button>
                <button
                  onClick={() => saveBotChat.mutate()}
                  disabled={saveBotChat.isPending || botChatDraft.trim() === wecomBotChatId}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:text-foreground disabled:opacity-40"
                >
                  {saveBotChat.isPending ? '保存中…' : '保存目标会话'}
                </button>
                <button
                  onClick={() => toggleBotConnection.mutate(!wecomBotEnabled)}
                  disabled={toggleBotConnection.isPending || !wecomBotId || !wecomBotSecret}
                  className="px-3 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:text-foreground disabled:opacity-40"
                  title={wecomBotEnabled ? '断开长连接' : '建立长连接'}
                >
                  {toggleBotConnection.isPending ? '处理中…' : (wecomBotEnabled ? '断开长连接' : '连接长连接')}
                </button>
                <TestSendButton test={testWecomBot} configured={wecomBotConfigured} />
                <TestResult test={testWecomBot} />
                {wecomBotId && <span className="text-[10px] text-emerald-500">● 凭证已配置</span>}
              </div>

              <details className="mt-3 text-[10px] text-muted">
                <summary className="cursor-pointer hover:text-secondary">如何获取 BotID 和 Secret?</summary>
                <ol className="mt-1.5 space-y-1 pl-4 list-decimal leading-relaxed">
                  <li>登录<b>企业微信管理后台</b> → 应用管理 → <b>智能机器人</b> → 创建机器人</li>
                  <li>填写名称、头像后进入机器人配置页</li>
                  <li>开启「<b>API 模式</b>」→ 选择「<b>长连接</b>」方式(另一项"回调URL"需公网IP)</li>
                  <li>页面显示 <b>BotID</b> 和 <b>Secret</b>, 复制填到上方输入框</li>
                </ol>
                <p className="mt-1.5 pl-4 text-muted/70">
                  💡 智能机器人支持 @交互和流式回复, 与群推送 Webhook(单向推送)互补。
                  保存后后端会自动建立 WebSocket 长连接保活。
                </p>
                <p className="mt-1.5 pl-4 text-muted/70">
                  📖 官方文档:
                  <a href="https://developer.work.weixin.qq.com/document/path/101463" target="_blank" rel="noreferrer" className="text-accent hover:text-accent/80">
                    智能机器人长连接 ↗
                  </a>
                </p>
              </details>
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}
