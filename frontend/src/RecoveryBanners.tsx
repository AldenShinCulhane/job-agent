import type { EngineStatus, LinkedInStatus } from './api'
import { retryText } from './engineStatus'

interface Props {
  engine: EngineStatus | null
  linkedin: LinkedInStatus | null
  onOpenSettings: () => void
  onOpenSearch: () => void
}

interface Banner {
  key: string
  tone: string
  text: string
  action?: { label: string; run: () => void }
}

/** Global recovery banners (PLAN.md M5): every autonomous-stop state the
 * app can be in gets one visible line with the way out — never a modal,
 * never an approval prompt. */
export default function RecoveryBanners({
  engine,
  linkedin,
  onOpenSettings,
  onOpenSearch,
}: Props) {
  const banners: Banner[] = []

  if (engine?.status === 'auth_required') {
    banners.push({
      key: 'engine-auth',
      tone: 'bg-rose-50 text-rose-800 border-rose-200',
      text: 'Claude sign-in required — scoring and package generation are paused until you log in.',
      action: { label: 'How to fix', run: onOpenSettings },
    })
  }
  if (engine?.status === 'config_error') {
    banners.push({
      key: 'engine-config',
      tone: 'bg-rose-50 text-rose-800 border-rose-200',
      text: `Claude engine configuration error${engine.last_error ? `: ${engine.last_error}` : ''}.`,
      action: { label: 'Details', run: onOpenSettings },
    })
  }
  if (engine?.status === 'waiting_usage_exhausted') {
    const retry = retryText(engine.retry_in_seconds)
    banners.push({
      key: 'engine-usage',
      tone: 'bg-amber-50 text-amber-800 border-amber-200',
      text: `Claude usage limit reached — work is paused and resumes automatically${retry ? ` in ${retry}` : ''}. Nothing is lost.`,
    })
  }
  if (linkedin?.state === 'captcha_stop') {
    banners.push({
      key: 'li-captcha',
      tone: 'bg-rose-50 text-rose-800 border-rose-200',
      text: 'LinkedIn checkpoint hit — solve it in the browser window, then resume the search. The app never bypasses checkpoints.',
      action: { label: 'Go to Search', run: onOpenSearch },
    })
  }
  if (linkedin?.state === 'logged_out') {
    banners.push({
      key: 'li-auth',
      tone: 'bg-amber-50 text-amber-800 border-amber-200',
      text: 'LinkedIn session expired — reconnect to keep searching. Scored jobs and packages are unaffected.',
      action: { label: 'Go to Search', run: onOpenSearch },
    })
  }

  if (banners.length === 0) return null
  return (
    <div>
      {banners.map((b) => (
        <div
          key={b.key}
          className={`flex items-center justify-between border-b px-8 py-2 text-sm ${b.tone}`}
        >
          <span>{b.text}</span>
          {b.action && (
            <button
              className="ml-4 shrink-0 rounded border border-current px-2 py-0.5 text-xs font-medium"
              onClick={b.action.run}
            >
              {b.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
