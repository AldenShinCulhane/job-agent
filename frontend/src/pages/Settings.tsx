import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { EngineStatus, Settings as SettingsPayload } from '../api'
import { engineBadge, retryText } from '../engineStatus'
import { useLatestOnly } from '../useLatestOnly'

const AUTH_LOGIN_CMD = 'uv run python -m backend.core.claude_cli auth login'

/** What this page owns from /api/settings. Its `engine` field is
 * deliberately dropped: engine state belongs to App, which already reads
 * /api/status on the event stream AND on the fallback poll that covers a
 * disconnected stream and the usage-limit countdown (a clock, so nothing
 * announces it). A second copy here would be a second owner with none of
 * that, and the two would drift apart on screen. */
type Documents = Omit<SettingsPayload, 'engine'>

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

  if (!documents) {
    return <p className="text-slate-400">{error ?? 'Loading…'}</p>
  }

  const badge = engine ? engineBadge(engine.status) : null
  const retry = retryText(engine?.retry_in_seconds ?? null)

  return (
    <div className="max-w-2xl space-y-6">
      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-lg font-semibold">Documents</h2>
        <div className="mt-4 flex items-center gap-3">
          <label className="text-sm font-medium" htmlFor="page-limit">
            Resume page limit
          </label>
          <input
            id="page-limit"
            type="number"
            min={1}
            step={1}
            className="w-20 rounded border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
            value={Number.isNaN(limit) ? '' : limit}
            disabled={saving}
            onChange={(e) => {
              limitSeq.current += 1
              setLimit(e.target.value === '' ? NaN : Number(e.target.value))
              setSaved(false)
            }}
          />
          <span className="text-sm text-slate-500">
            page{limit === 1 ? '' : 's'}
          </span>
          <button
            className="rounded bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-40"
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
          {saved && <span className="text-sm text-emerald-700">Saved</span>}
        </div>
        <p className="mt-2 text-sm text-slate-500">
          Applies to packages you generate from now on. Packages already
          created keep the limit they were generated with. Cover letters are
          always exactly one page.
        </p>
        {error && <p className="mt-2 text-sm text-rose-600">{error}</p>}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Claude engine</h2>
          {badge && (
            <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}>
              {badge.label}
            </span>
          )}
        </div>
        {engine ? (
          <>
            <dl className="mt-4 space-y-1 text-sm">
              <div className="flex gap-2">
                <dt className="w-28 text-slate-500">Model</dt>
                <dd>{engine.model ?? 'not pinned yet'}</dd>
              </div>
              {retry && (
                <div className="flex gap-2">
                  <dt className="w-28 text-slate-500">Auto-resume</dt>
                  <dd>in {retry}</dd>
                </div>
              )}
              {engine.last_error && (
                <div className="flex gap-2">
                  <dt className="w-28 text-slate-500">Last error</dt>
                  <dd className="text-rose-700">{engine.last_error}</dd>
                </div>
              )}
            </dl>
            {engine.status === 'auth_required' && (
              <p className="mt-3 rounded bg-rose-50 p-3 text-sm text-rose-800">
                Sign in from a terminal in the project folder, then re-check:
                <code className="mt-1 block rounded bg-white px-2 py-1 font-mono text-xs">
                  {AUTH_LOGIN_CMD}
                </code>
              </p>
            )}
          </>
        ) : (
          <p className="mt-4 text-sm text-slate-400">Checking the engine…</p>
        )}
        <div className="mt-4 flex items-center gap-3">
          <button
            className="rounded border border-slate-300 px-3 py-1 text-sm font-medium disabled:opacity-40"
            disabled={probing}
            onClick={() => void reprobe()}
          >
            {probing ? 'Checking…' : 'Re-check engine'}
          </button>
          {probeNote && <span className="text-sm text-slate-600">{probeNote}</span>}
        </div>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-6">
        <h2 className="text-lg font-semibold">LaTeX engines</h2>
        {documents.latex_engines.length > 0 ? (
          <p className="mt-2 text-sm text-slate-600">
            Available: {documents.latex_engines.join(', ')} — the engine that
            compiled each package is recorded in its provenance (pagination
            can differ between engines).
          </p>
        ) : (
          <p className="mt-2 rounded bg-amber-50 p-3 text-sm text-amber-800">
            No LaTeX engine found. Install tectonic (single binary) or a TeX
            distribution providing pdflatex, then re-run{' '}
            <code className="font-mono text-xs">scripts/setup.sh</code>.
          </p>
        )}
      </section>
    </div>
  )
}
