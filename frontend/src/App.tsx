import { useEffect, useState } from 'react'

interface EngineStatus {
  status: string
  model: string | null
  retry_in_seconds: number | null
  last_error: string | null
}

interface AppStatus {
  app: string
  engine: EngineStatus
}

const STATUS_LABELS: Record<string, { label: string; tone: string }> = {
  ok: { label: 'Ready', tone: 'bg-emerald-100 text-emerald-800' },
  unprobed: { label: 'Not yet probed', tone: 'bg-slate-100 text-slate-600' },
  waiting_usage_exhausted: {
    label: 'Waiting — usage limit reached',
    tone: 'bg-amber-100 text-amber-800',
  },
  auth_required: {
    label: 'Sign in to Claude required',
    tone: 'bg-rose-100 text-rose-800',
  },
  config_error: { label: 'Engine configuration error', tone: 'bg-rose-100 text-rose-800' },
}

export default function App() {
  const [status, setStatus] = useState<AppStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/status')
      .then((r) => r.json())
      .then(setStatus)
      .catch((e) => setError(String(e)))
  }, [])

  const engine = status?.engine
  const badge = engine ? STATUS_LABELS[engine.status] ?? { label: engine.status, tone: 'bg-slate-100 text-slate-600' } : null

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white px-8 py-4">
        <h1 className="text-xl font-semibold">Job Agent</h1>
        <p className="text-sm text-slate-500">
          LinkedIn job search with tailored resume + cover letter packages
        </p>
      </header>
      <main className="mx-auto max-w-3xl p-8">
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-sm font-medium uppercase tracking-wide text-slate-500">
            Claude engine
          </h2>
          {error && <p className="text-rose-600">Backend unreachable: {error}</p>}
          {!error && !status && <p className="text-slate-400">Loading…</p>}
          {badge && engine && (
            <div className="flex items-center gap-3">
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}>
                {badge.label}
              </span>
              {engine.model && (
                <span className="font-mono text-sm text-slate-600">{engine.model}</span>
              )}
            </div>
          )}
          {engine?.last_error && (
            <p className="mt-3 text-sm text-slate-500">{engine.last_error}</p>
          )}
        </section>
      </main>
    </div>
  )
}
