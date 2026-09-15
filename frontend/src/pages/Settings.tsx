import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { EngineStatus, Settings as SettingsPayload } from '../api'
import { engineBadge, retryText } from '../engineStatus'
import { btnPrimary, btnSecondary, chip } from '../styles'
import { usePreference } from '../theme'
import type { ThemePreference } from '../theme'
import { useLatestOnly } from '../useLatestOnly'

const AUTH_LOGIN_CMD = 'uv run python -m backend.core.claude_cli auth login'

/** What this page owns from /api/settings. Its `engine` field is
 * deliberately dropped: engine state belongs to App, which already reads
 * /api/status on the event stream AND on the fallback poll that covers a
 * disconnected stream and the usage-limit countdown (a clock, so nothing
 * announces it). A second copy here would be a second owner with none of
 * that, and the two would drift apart on screen. */
type Documents = Omit<SettingsPayload, 'engine'>

const APPEARANCE: { value: ThemePreference; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

export default function Settings({
  engine,
  refreshEngine,
}: {
  engine: EngineStatus | null
  refreshEngine: () => Promise<unknown>
}) {
  const [documents, setDocuments] = useState<Documents | null>(null)
  const [limit, setLimit] = useState(1)
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeNote, setProbeNote] = useState<string | null>(null)
  // Bumped by every keystroke in the page-limit box. A read issued before
  // one of those must not push the stored value back over what the user
  // has since typed — the save's own reload is exactly that read.
  const limitSeq = useRef(0)
  // Browser-local, never on the server: it belongs to this machine's
  // screen, not to the profile.
  const [appearance, setAppearance] = usePreference()

  const readDocuments = useLatestOnly()

  const loadDocuments = useCallback(() => {
    const seq = limitSeq.current
    return readDocuments(
      () => api.getSettings(),
      (s) => {
        setDocuments(s)
        if (limitSeq.current === seq) setLimit(s.resume_page_limit)
      },
    ).catch((e) => setError(String(e)))
  }, [readDocuments])

  useEffect(() => {
    void loadDocuments()
  }, [loadDocuments])

  const save = async () => {
    setError(null)
    setSaved(false)
    setSaving(true)
    try {
      await api.putSettings(limit)
      setSaved(true)
      await loadDocuments()
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const reprobe = async () => {
    setProbing(true)
    setProbeNote(null)
    try {
      const res = await api.reprobeEngine()
      setProbeNote(
        res.ok
          ? `Engine healthy — ${res.model ?? 'model pinned'}`
          : `${res.error ?? 'Engine problem'}: ${res.detail ?? 'see status'}`,
      )
      await refreshEngine()
    } catch (e) {
      setProbeNote(String(e))
    } finally {
      setProbing(false)
    }
  }

  const badge = engine ? engineBadge(engine.status) : null
  const retry = retryText(engine?.retry_in_seconds ?? null)
  // Each section stands on its own: a failed /api/settings read takes out
  // the two sections that need it and nothing else, so the engine
  // recovery instructions and the appearance control stay reachable.
  const documentsPending = (
    <p className="mt-4 text-sm text-ink-3">{error ?? 'Loading…'}</p>
  )

  return (
    <div className="max-w-2xl space-y-6">
      <section className="rounded-lg border border-line bg-surface p-6">
        <h2 className="text-lg font-semibold">Documents</h2>
        {documents ? (
          <>
            <div className="mt-4 flex items-center gap-3">
              <label className="text-sm font-medium" htmlFor="page-limit">
                Resume page limit
              </label>
              <input
                id="page-limit"
                type="number"
                min={1}
                step={1}
                className="w-20 rounded border border-line-strong bg-surface px-2 py-1 text-sm text-ink disabled:opacity-50"
                value={Number.isNaN(limit) ? '' : limit}
                disabled={saving}
                onChange={(e) => {
                  limitSeq.current += 1
                  setLimit(e.target.value === '' ? NaN : Number(e.target.value))
                  setSaved(false)
                }}
              />
              <span className="text-sm text-ink-3">
                page{limit === 1 ? '' : 's'}
              </span>
              <button
                className={`${btnPrimary} rounded px-3 py-1 text-sm font-medium`}
                disabled={
                  saving ||
                  !Number.isInteger(limit) ||
                  limit < 1 ||
                  limit === documents.resume_page_limit
                }
                onClick={() => void save()}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              {saved && <span className="text-sm text-ok-fg">Saved</span>}
            </div>
            <p className="mt-2 text-sm text-ink-3">
              Applies to packages you generate from now on. Packages already
              created keep the limit they were generated with. Cover letters are
              always exactly one page.
            </p>
            {error && <p className="mt-2 text-sm text-danger-fg">{error}</p>}
          </>
        ) : (
          documentsPending
        )}
      </section>

      <section className="rounded-lg border border-line bg-surface p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Claude engine</h2>
          {badge && (
            <span className={`rounded-full px-3 py-1 text-sm font-medium ${chip[badge.tone]}`}>
              {badge.label}
            </span>
          )}
        </div>
        {engine ? (
          <>
            <dl className="mt-4 space-y-1 text-sm">
              <div className="flex gap-2">
                <dt className="w-28 text-ink-3">Model</dt>
                <dd>{engine.model ?? 'not pinned yet'}</dd>
              </div>
              {retry && (
                <div className="flex gap-2">
                  <dt className="w-28 text-ink-3">Auto-resume</dt>
                  <dd>in {retry}</dd>
                </div>
              )}
              {engine.last_error && (
                <div className="flex gap-2">
                  <dt className="w-28 text-ink-3">Last error</dt>
                  <dd className="text-danger-fg">{engine.last_error}</dd>
                </div>
              )}
            </dl>
            {engine.status === 'auth_required' && (
              <p className="mt-3 rounded bg-danger-bg p-3 text-sm text-danger-fg">
                Sign in from a terminal in the project folder, then re-check:
                <code className="mt-1 block rounded bg-surface px-2 py-1 font-mono text-xs">
                  {AUTH_LOGIN_CMD}
                </code>
              </p>
            )}
          </>
        ) : (
          <p className="mt-4 text-sm text-ink-3">Checking the engine…</p>
        )}
        <div className="mt-4 flex items-center gap-3">
          <button
            className={`${btnSecondary} rounded px-3 py-1 text-sm font-medium`}
            disabled={probing}
            onClick={() => void reprobe()}
          >
            {probing ? 'Checking…' : 'Re-check engine'}
          </button>
          {probeNote && <span className="text-sm text-ink-2">{probeNote}</span>}
        </div>
      </section>

      <section className="rounded-lg border border-line bg-surface p-6">
        <h2 className="text-lg font-semibold">LaTeX engines</h2>
        {documents ? (
          documents.latex_engines.length > 0 ? (
            <p className="mt-2 text-sm text-ink-2">
              Available: {documents.latex_engines.join(', ')} — the engine that
              compiled each package is recorded in its provenance (pagination
              can differ between engines).
            </p>
          ) : (
            <p className="mt-2 rounded bg-warn-bg p-3 text-sm text-warn-fg">
              No LaTeX engine found. Install tectonic (single binary) or a TeX
              distribution providing pdflatex, then re-run{' '}
              <code className="font-mono text-xs">scripts/setup.sh</code>.
            </p>
          )
        ) : (
          documentsPending
        )}
      </section>

      <section className="rounded-lg border border-line bg-surface p-6">
        <fieldset>
          <legend className="text-lg font-semibold">Appearance</legend>
          <p className="mt-2 text-sm text-ink-3">
            Kept in this browser. System follows the operating system setting
            and changes with it.
          </p>
          <div className="mt-4 flex flex-wrap gap-5">
            {APPEARANCE.map((option) => (
              <label key={option.value} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="appearance"
                  value={option.value}
                  checked={appearance === option.value}
                  onChange={() => setAppearance(option.value)}
                />
                {option.label}
              </label>
            ))}
          </div>
        </fieldset>
      </section>
    </div>
  )
}
