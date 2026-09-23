import type { EngineStatus, EnginesView, Provider, ProviderView } from './api'
import { badgeFor } from './styles.ts'
import type { Badge } from './styles.ts'

export const PROVIDER_NAME: Record<Provider, string> = {
  claude: 'Claude',
  codex: 'Codex',
}

/** The subscription each engine runs on: the user's own, never a key. */
export const PROVIDER_PLAN: Record<Provider, string> = {
  claude: 'Claude subscription (claude.ai)',
  codex: 'ChatGPT subscription',
}

/** Who receives the prompts — resume and job postings — for each engine. */
export const PROVIDER_VENDOR: Record<Provider, string> = {
  claude: 'Anthropic',
  codex: 'OpenAI',
}

/** Run from a terminal in the project folder. Each engine keeps its own
 * sign-in inside the app's data folder, apart from any other install. */
export const LOGIN_COMMAND: Record<Provider, string> = {
  claude: 'uv run python -m backend.core.claude_cli auth login',
  codex: 'uv run python -m backend.core.codex_cli login',
}

export function providerName(provider: string): string {
  return Object.hasOwn(PROVIDER_NAME, provider)
    ? PROVIDER_NAME[provider as Provider]
    : provider
}

export function otherProvider(provider: Provider): Provider {
  return provider === 'claude' ? 'codex' : 'claude'
}

/** The selected engine: what the header badge and the banners show. */
export function selectedEngine(view: EnginesView): ProviderView {
  return view.providers[view.selection.provider]
}

export function engineBadge(engine: Pick<EngineStatus, 'status' | 'provider'>): Badge {
  const name = providerName(engine.provider)
  return badgeFor(
    {
      ok: { label: `${name} ready`, tone: 'ok' },
      unprobed: { label: `${name} not checked yet`, tone: 'neutral' },
      not_entitled: { label: 'Model not on your plan', tone: 'danger' },
      waiting_usage_exhausted: { label: `${name} usage limit reached`, tone: 'warn' },
      auth_required: { label: `Sign in to ${name} required`, tone: 'danger' },
      config_error: { label: `${name} setup problem`, tone: 'danger' },
    },
    engine.status,
  )
}

/** A model's display name from its provider's list, else its id. */
export function modelLabel(
  view: EnginesView | null,
  provider: Provider,
  id: string | null,
): string | null {
  if (!id) return null
  const entry = view?.providers[provider]?.catalog?.find((m) => m.id === id)
  return entry?.label ?? id
}

/** What the model a selection names is called, for sentences. Claude's
 * default names the model it settled on once it has. */
export function selectionText(view: EnginesView): string {
  const { provider, model } = view.selection
  if (model) return modelLabel(view, provider, model) ?? model
  const settled = view.providers.claude.auto_model ?? null
  return settled
    ? `the default (${modelLabel(view, 'claude', settled)})`
    : 'the default (Fable 5 if your plan has it, else Opus 5)'
}

// Mirrors the server's check (backend/core/llm.py): model ids travel into
// command-line arguments and files, so only a conservative shape is taken.
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/[\]-]{0,127}$/

export function validModelId(id: string): boolean {
  return MODEL_ID.test(id)
}

export function retryText(seconds: number | null): string | null {
  if (seconds == null || seconds <= 0) return null
  const mins = Math.ceil(seconds / 60)
  return mins >= 60
    ? `~${Math.floor(mins / 60)}h ${mins % 60}m`
    : `~${mins}m`
}

/** A moment as a clock time — "3:00 PM" — with the day in front when it
 * is not today. Null for a missing or unreadable time. */
export function clockText(iso: string | null, now: Date = new Date()): string | null {
  if (!iso) return null
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  if (at.toDateString() === now.toDateString()) return time
  const day = at.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
  return `${day}, ${time}`
}

/** How long ago, coarsely: "just now", "12 min ago", "3 h ago", else the
 * date. */
export function agoText(iso: string | null, now: number = Date.now()): string | null {
  if (!iso) return null
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return null
  const mins = Math.floor((now - at) / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} h ago`
  return new Date(at).toLocaleDateString([], { month: 'short', day: 'numeric' })
}
