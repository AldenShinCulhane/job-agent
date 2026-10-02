import type { EnginesView, LinkedInStatus } from './api'
import {
  clockText,
  modelLabel,
  otherProvider,
  providerName,
  retryText,
  selectedEngine,
} from './engineStatus'
import { banner, column } from './styles'
import type { Tone } from './styles'

interface Props {
  engines: EnginesView | null
  linkedin: LinkedInStatus | null
  onOpenEngine: () => void
  onOpenSearch: () => void
}

interface Banner {
  key: string
  tone: Tone
  text: string
  action?: { label: string; run: () => void }
}

/** Global recovery banners (PLAN.md M5): every autonomous-stop state the
 * app can be in gets one visible line with the way out — never a modal,
 * never an approval prompt. */
export default function RecoveryBanners({
  engines,
  linkedin,
  onOpenEngine,
  onOpenSearch,
}: Props) {
  const banners: Banner[] = []
  const engine = engines ? selectedEngine(engines) : null
  const name = engine ? providerName(engine.provider) : ''

  if (engine?.status === 'auth_required') {
    banners.push({
      key: 'engine-auth',
      tone: 'danger',
      text: `${name} sign-in required — scoring and package generation are paused until you sign in.`,
      action: { label: 'How to fix', run: onOpenEngine },
    })
  }
  if (engine?.status === 'config_error') {
    banners.push({
      key: 'engine-config',
      tone: 'danger',
      text: `${name} engine configuration error${engine.last_error ? `: ${engine.last_error}` : ''}.`,
      action: { label: 'Details', run: onOpenEngine },
    })
  }
  if (engines && engine?.status === 'not_entitled') {
    const model = modelLabel(engines, engine.provider, engine.model) ?? 'The selected model'
    banners.push({
      key: 'engine-model',
      tone: 'danger',
      text: `${model} isn't available on your ${name} plan — choose another model. Scoring and package generation wait until you do.`,
      action: { label: 'Choose model', run: onOpenEngine },
    })
  }
  if (engines && engine?.status === 'unavailable') {
    const model = modelLabel(engines, engine.provider, engine.model) ?? 'The selected model'
    banners.push({
      key: 'engine-lockdown',
      tone: 'danger',
      text: `${model} can't run under the app's safety lockdown on this version of ${name} — choose another model. Scoring and package generation wait until you do.`,
      action: { label: 'Choose model', run: onOpenEngine },
    })
  }
  if (engines && engine?.status === 'waiting_usage_exhausted') {
    const retry = retryText(engine.retry_in_seconds)
    const other = otherProvider(engine.provider)
    const otherName = providerName(other)
    const signedIn = engines.providers[other].credentials?.present === true
    const reset = clockText(engine.limit_resets_at)
    // Honest scope: queued scoring/package work has a retry timer and
    // is never lost; one-off actions (upload, Explain, Re-rank) are
    // request-scoped — they fail now and the user retries them.
    const wait = retry
      ? `queued scoring and package work resumes on its own in ${retry}${reset ? ` (${name} says the limit resets at ${reset})` : ''} and is never lost`
      : 'the wait is over, so queued scoring and package work retries on its own; run any action of yours that failed again'
    const bothLimited = engines.providers[other].status === 'waiting_usage_exhausted'
    const way = bothLimited
      ? `${otherName} is at its limit too, so for now`
      : `To keep working now, ${signedIn ? `switch to a ${otherName} model` : `sign in to ${otherName} and switch to one of its models`} — another ${name} model doesn't help, the limit covers them all. Or wait:`
    banners.push({
      key: 'engine-usage',
      tone: 'warn',
      text: `${name} usage limit reached. ${way} ${wait}.`,
      action: { label: 'Switch model', run: onOpenEngine },
    })
  }
  if (linkedin?.state === 'captcha_stop') {
    banners.push({
      key: 'li-captcha',
      tone: 'danger',
      text: 'LinkedIn checkpoint hit — solve it in the browser window, then press Connect; the search resumes on its own. The app never bypasses checkpoints.',
      action: { label: 'Go to Search', run: onOpenSearch },
    })
  }
  if (linkedin?.state === 'logged_out') {
    banners.push({
      key: 'li-auth',
      tone: 'warn',
      text: 'LinkedIn session expired — reconnect to keep searching. Scored jobs and packages are unaffected.',
      action: { label: 'Go to Search', run: onOpenSearch },
    })
  }

  if (banners.length === 0) return null
  return (
    // A named region: page content outside every landmark is content a
    // screen-reader user navigating by landmark never reaches.
    <section aria-label="Recovery notices">
      {banners.map((b) => (
        <div key={b.key} className={`border-b text-sm ${banner[b.tone]}`}>
          <div className={`${column} flex items-center justify-between py-2`}>
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
        </div>
      ))}
    </section>
  )
}
