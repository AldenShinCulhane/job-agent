import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { api, PROVIDERS } from '../api'
import type { EnginesView, ModelOption, Provider, ProviderView } from '../api'
import {
  agoText,
  clockText,
  engineBadge,
  LOGIN_COMMAND,
  modelLabel,
  otherProvider,
  PROVIDER_NAME,
  PROVIDER_PLAN,
  PROVIDER_VENDOR,
  retryText,
  selectedEngine,
  selectionText,
  validModelId,
} from '../engineStatus'
import { banner, btnPrimary, btnSecondary, chip, inputCls, labelCls, note } from '../styles'
import type { Tone } from '../styles'

const CUSTOM = 'custom'

/** A selection as one <select> value: "provider:model", with an empty
 * model for Claude's default. */
function keyOf(provider: Provider, model: string | null): string {
  return `${provider}:${model ?? ''}`
}

function parseKey(key: string): { provider: Provider; model: string | null } | null {
  const at = key.indexOf(':')
  const provider = key.slice(0, at)
  if (!(PROVIDERS as readonly string[]).includes(provider)) return null
  return { provider: provider as Provider, model: key.slice(at + 1) || null }
}

/** Why a provider's models can't answer right now, for its group label. */
function groupNote(view: ProviderView): string {
  if (view.status === 'waiting_usage_exhausted') return ' — usage limit reached'
  if (view.status === 'auth_required' || view.credentials?.present === false) {
    return ' — sign in first'
  }
  if (view.status === 'config_error') return ' — setup problem'
  return ''
}

function optionText(view: ProviderView, model: ModelOption): string {
  const state = view.models[model.id]?.status
  if (state === 'not_entitled') return `${model.label} — not on your plan`
  if (state === 'unavailable') return `${model.label} — unavailable`
  return model.label
}

/** A model that failed the app's lockdown check: listed, never usable. */
function unavailable(view: ProviderView, id: string): boolean {
  return view.models[id]?.status === 'unavailable'
}

interface Note {
  tone: Tone
  text: string
}

export default function EnginePicker({
  engines,
  refreshEngine,
  headingRef,
}: {
  engines: EnginesView | null
  refreshEngine: () => Promise<unknown>
  /** Where focus lands when the app opens this section by itself. */
  headingRef: RefObject<HTMLHeadingElement | null>
}) {
  // What the user has picked but not applied; null follows the selection
  // the server has, so a change made elsewhere shows up here.
  const [draft, setDraft] = useState<string | null>(null)
  const [customProvider, setCustomProvider] = useState<Provider>('claude')
  const [customId, setCustomId] = useState('')
  const [switching, setSwitching] = useState(false)
  const [probing, setProbing] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [message, setMessage] = useState<Note | null>(null)

  // A provider's model list is read once per visit when none is known
  // yet. No prompt is spent: the CLIs list their models locally.
  const listed = useRef(new Set<Provider>())
  useEffect(() => {
    if (!engines) return
    const missing = PROVIDERS.filter(
      (p) =>
        !listed.current.has(p) &&
        engines.providers[p].catalog === null &&
        engines.providers[p].catalog_error === null,
    )
    if (missing.length === 0) return
    missing.forEach((p) => listed.current.add(p))
    void Promise.allSettled(missing.map((p) => api.refreshCatalog(p))).then(() =>
      refreshEngine(),
    )
  }, [engines, refreshEngine])

  if (!engines) {
    return (
      <section className="rounded-lg border border-line bg-surface p-6" aria-labelledby="engine-heading">
        <h2 id="engine-heading" ref={headingRef} tabIndex={-1} className="text-lg font-semibold">
          AI engine
        </h2>
        <p className="mt-4 text-sm text-ink-3">Checking the engine…</p>
      </section>
    )
  }

  const engine = selectedEngine(engines)
  const name = PROVIDER_NAME[engine.provider]
  const badge = engineBadge(engine)
  const current = keyOf(engines.selection.provider, engines.selection.model)
  const choice = draft ?? current
  const picked = choice === CUSTOM ? null : parseKey(choice)
  // A typed-in id the list already marks unavailable is refused here as
  // the list refuses it (and the server refuses it too).
  const customLocked =
    choice === CUSTOM && unavailable(engines.providers[customProvider], customId.trim())
  const target =
    choice === CUSTOM
      ? validModelId(customId.trim()) && !customLocked
        ? { provider: customProvider, model: customId.trim() }
        : null
      : picked
  const changed = target !== null && keyOf(target.provider, target.model) !== current
  const verified = agoText(engine.verified_at)

  const apply = async () => {
    if (!target) return
    const label =
      target.model === null
        ? 'the Claude default'
        : (modelLabel(engines, target.provider, target.model) ?? target.model)
    setSwitching(true)
    setMessage(null)
    try {
      const res = await api.selectEngine(target.provider, target.model)
      setMessage(
        res.ok
          ? {
              tone: 'ok',
              text: `Every task now uses ${label}${res.verified ? ' — checked, and it answers' : ''}.`,
            }
          : {
              tone: 'warn',
              text: `${label} is selected but can't run yet: ${res.detail ?? res.error ?? 'see the engine status'}. Choose another model, or fix it and re-check.`,
            },
      )
      setDraft(null)
      setCustomId('')
    } catch (e) {
      setMessage({ tone: 'danger', text: String(e) })
    } finally {
      setSwitching(false)
      await refreshEngine()
    }
  }

  const reprobe = async () => {
    setProbing(true)
    setMessage(null)
    try {
      const res = await api.reprobeEngine()
      setMessage(
        res.ok
          ? { tone: 'ok', text: `Engine healthy — ${res.model ?? 'model settled'}` }
          : { tone: 'danger', text: `${res.error ?? 'Engine problem'}: ${res.detail ?? 'see status'}` },
      )
    } catch (e) {
      setMessage({ tone: 'danger', text: String(e) })
    } finally {
      setProbing(false)
      await refreshEngine()
    }
  }

  const refreshLists = async () => {
    setRefreshing(true)
    try {
      await Promise.allSettled(PROVIDERS.map((p) => api.refreshCatalog(p)))
    } finally {
      setRefreshing(false)
      await refreshEngine()
    }
  }

  // The current selection is always offered, even when it is a custom id
  // its provider does not list.
  const offered = (p: Provider): ModelOption[] => {
    const catalog = engines.providers[p].catalog ?? []
    const { provider, model } = engines.selection
    if (provider === p && model && !catalog.some((m) => m.id === model)) {
      return [...catalog, { id: model, label: model, description: '', efforts: [] }]
    }
    return catalog
  }
  const autoModel = engines.providers.claude.auto_model ?? null
  // Listed models that failed the lockdown, other than the selection (its
  // own notice above says so).
  const lockedOut = offered('codex').filter(
    (m) => unavailable(engines.providers.codex, m.id) && keyOf('codex', m.id) !== current,
  )
  const listErrors = PROVIDERS.filter((p) => engines.providers[p].catalog_error)

  return (
    <section className="rounded-lg border border-line bg-surface p-6" aria-labelledby="engine-heading">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="engine-heading" ref={headingRef} tabIndex={-1} className="text-lg font-semibold">
          AI engine
        </h2>
        <span className={`rounded-full px-3 py-1 text-sm font-medium ${chip[badge.tone]}`}>
          {badge.label}
        </span>
      </div>
      <p className="mt-1 text-sm text-ink-3">
        One model does every task — reading your resume, scoring jobs, writing packages — on
        your own subscription. The app never switches models by itself.
      </p>

      {engine.status === 'waiting_usage_exhausted' && (
        <UsageNotice engines={engines} engine={engine} />
      )}
      {engine.status === 'not_entitled' && (
        <div className={`mt-4 rounded-md border p-4 text-sm ${banner.danger}`}>
          <p className="font-medium">
            {modelLabel(engines, engine.provider, engine.model) ?? 'This model'} isn't available on
            your {name} plan.
          </p>
          <p className="mt-1">Choose another model below. Scoring and packages wait until you do.</p>
        </div>
      )}
      {engine.status === 'unavailable' && (
        <div className={`mt-4 rounded-md border p-4 text-sm ${banner.danger}`}>
          <p className="font-medium">
            {modelLabel(engines, engine.provider, engine.model) ?? 'This model'} can't run under
            the app's safety lockdown on this version of {name}.
          </p>
          <p className="mt-1">Choose another model below. Scoring and packages wait until you do.</p>
          {engine.last_error && <p className="mt-1 break-words text-xs">{engine.last_error}</p>}
        </div>
      )}

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {PROVIDERS.map((p) => (
          <ProviderCard
            key={p}
            provider={p}
            view={engines.providers[p]}
            inUse={engines.selection.provider === p}
          />
        ))}
      </div>

      <div className="mt-5">
        <label htmlFor="engine-model" className={labelCls}>
          Model for every task
        </label>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <select
            id="engine-model"
            className={`${inputCls} w-auto max-w-full`}
            value={choice}
            disabled={switching}
            onChange={(e) => {
              setDraft(e.target.value === current ? null : e.target.value)
              setMessage(null)
            }}
          >
            {PROVIDERS.map((p) => (
              <optgroup key={p} label={`${PROVIDER_NAME[p]}${groupNote(engines.providers[p])}`}>
                {p === 'claude' && (
                  <option value={keyOf('claude', null)}>
                    {autoModel
                      ? `Default — ${modelLabel(engines, 'claude', autoModel)}`
                      : 'Default — Fable 5 if your plan has it, else Opus 5'}
                  </option>
                )}
                {offered(p).map((m) => (
                  <option
                    key={m.id}
                    value={keyOf(p, m.id)}
                    disabled={
                      unavailable(engines.providers[p], m.id) && keyOf(p, m.id) !== current
                    }
                  >
                    {optionText(engines.providers[p], m)}
                  </option>
                ))}
              </optgroup>
            ))}
            <option value={CUSTOM}>Another model ID…</option>
          </select>
          <button
            className={`${btnPrimary} rounded px-3 py-1 text-sm font-medium`}
            disabled={!changed || switching}
            onClick={() => void apply()}
          >
            {switching ? 'Checking the model…' : 'Use this model'}
          </button>
        </div>
        {choice === CUSTOM && (
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor="engine-custom-provider" className={labelCls}>
                Engine
              </label>
              <select
                id="engine-custom-provider"
                className={`${inputCls} mt-1 w-auto`}
                value={customProvider}
                onChange={(e) => setCustomProvider(e.target.value as Provider)}
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {PROVIDER_NAME[p]}
                  </option>
                ))}
              </select>
            </div>
            <div className="min-w-48 flex-1">
              <label htmlFor="engine-custom-id" className={labelCls}>
                Model ID
              </label>
              <input
                id="engine-custom-id"
                className={`${inputCls} mt-1`}
                value={customId}
                placeholder={customProvider === 'claude' ? 'claude-sonnet-5' : 'gpt-5.5'}
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => setCustomId(e.target.value)}
              />
            </div>
            {customId.trim() !== '' && !validModelId(customId.trim()) && (
              <p className={`w-full text-sm ${note.danger}`}>
                Letters, digits and . _ : / - [ ] only, starting with a letter or digit.
              </p>
            )}
            {customLocked && (
              <p className={`w-full text-sm ${note.danger}`}>
                That model failed the app's safety check on this version of{' '}
                {PROVIDER_NAME[customProvider]}, so it can't be chosen.
              </p>
            )}
          </div>
        )}
        <p className="mt-2 text-sm text-ink-3">
          Every task uses {selectionText(engines)} on {name}.{' '}
          {verified ? `Last answered ${verified}.` : 'Not checked yet on this plan.'}
        </p>
        {lockedOut.length > 0 && (
          <p className="mt-1 text-sm text-ink-3">
            {lockedOut.map((m) => m.label).join(', ')} can't run under the app's safety
            lockdown on this version of Codex. Refresh model lists checks again after a Codex
            update.
          </p>
        )}
        {listErrors.map((p) => (
          <p key={p} className={`mt-1 text-sm ${note.warn}`}>
            Couldn't list {PROVIDER_NAME[p]} models ({engines.providers[p].catalog_error}); you can
            still enter a model ID.
          </p>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          className={`${btnSecondary} rounded px-3 py-1 text-sm font-medium`}
          disabled={probing}
          onClick={() => void reprobe()}
        >
          {probing ? 'Checking…' : 'Re-check engine'}
        </button>
        <button
          className={`${btnSecondary} rounded px-3 py-1 text-sm font-medium`}
          disabled={refreshing}
          onClick={() => void refreshLists()}
        >
          {refreshing ? 'Refreshing…' : 'Refresh model lists'}
        </button>
      </div>
      {message && (
        <p role="status" className={`mt-3 text-sm ${note[message.tone]}`}>
          {message.text}
        </p>
      )}
    </section>
  )
}

function ProviderCard({
  provider,
  view,
  inUse,
}: {
  provider: Provider
  view: ProviderView
  inUse: boolean
}) {
  const credentials = view.credentials
  const signedOut = credentials?.present === false || view.status === 'auth_required'
  const problem = credentials?.problem ?? (view.status === 'auth_required' ? view.last_error : null)
  const waiting = view.status === 'waiting_usage_exhausted'
  const reset = clockText(view.limit_resets_at)
  return (
    <div className={`rounded-md border p-4 ${inUse ? 'border-accent' : 'border-line'}`}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium">{PROVIDER_NAME[provider]}</h3>
        {inUse && <span className={`rounded-full px-2 py-0.5 text-xs ${chip.info}`}>In use</span>}
      </div>
      <p className="text-xs text-ink-3">
        {PROVIDER_PLAN[provider]} · prompts go to {PROVIDER_VENDOR[provider]}
      </p>
      {signedOut ? (
        <div className="mt-2 text-sm">
          <p className={note.danger}>{problem ?? 'Not signed in.'}</p>
          <p className="mt-1 text-ink-2">Sign in from a terminal in the project folder:</p>
          <code className="mt-1 block overflow-x-auto rounded bg-surface-2 px-2 py-1 font-mono text-xs text-ink">
            {LOGIN_COMMAND[provider]}
          </code>
        </div>
      ) : (
        <p className={`mt-2 text-sm ${credentials?.present ? note.ok : note.neutral}`}>
          {credentials?.present ? 'Signed in' : 'Sign-in not checked yet'}
        </p>
      )}
      {waiting && (
        <p className={`mt-2 text-sm ${note.warn}`}>
          Usage limit reached ·{' '}
          {reset ? `resets ${reset}` : 'reset time unknown'}
        </p>
      )}
    </div>
  )
}

function UsageNotice({ engines, engine }: { engines: EnginesView; engine: ProviderView }) {
  const name = PROVIDER_NAME[engine.provider]
  const other = otherProvider(engine.provider)
  const otherName = PROVIDER_NAME[other]
  const otherView = engines.providers[other]
  const retry = retryText(engine.retry_in_seconds)
  const retryAt = clockText(engine.next_retry_at)
  const reset = clockText(engine.limit_resets_at)
  const advice =
    otherView.status === 'waiting_usage_exhausted'
      ? `${otherName} is at its limit too. Queued work resumes on its own when a limit lifts.`
      : otherView.credentials?.present === true
        ? `Pick a ${otherName} model below and press Use this model. Another ${name} model won't help: the limit covers every ${name} model.`
        : `Sign in to ${otherName} (below), then pick one of its models. Another ${name} model won't help: the limit covers every ${name} model.`
  return (
    <div className={`mt-4 rounded-md border p-4 text-sm ${banner.warn}`}>
      <p className="font-medium">
        {name} usage limit reached — switch models to keep working now.
      </p>
      <p className="mt-1">{advice}</p>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="font-medium">Limit resets</dt>
        <dd>{reset ? `${reset}, as ${name} reported` : `unknown — ${name} didn't say`}</dd>
        <dt className="font-medium">Next automatic retry</dt>
        <dd>
          {retry
            ? `in ${retry}${retryAt ? ` (${retryAt})` : ''} — the app's estimate`
            : 'due now — queued work retries on its own'}
        </dd>
      </dl>
      <p className="mt-2">Nothing queued is lost while you wait.</p>
    </div>
  )
}
