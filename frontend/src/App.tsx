import { useEffect, useState } from 'react'
import { api } from './api'
import type { EngineStatus } from './api'
import Onboarding from './pages/Onboarding'
import Search from './pages/Search'

const TABS = [
  { id: 'profile', label: 'Profile' },
  { id: 'search', label: 'Search' },
] as const

type TabId = (typeof TABS)[number]['id']

const STATUS_LABELS: Record<string, { label: string; tone: string }> = {
  ok: { label: 'Claude ready', tone: 'bg-emerald-100 text-emerald-800' },
  unprobed: { label: 'Claude not yet probed', tone: 'bg-slate-100 text-slate-600' },
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
  const [engine, setEngine] = useState<EngineStatus | null>(null)
  const [tab, setTab] = useState<TabId>('profile')

  useEffect(() => {
    api
      .status()
      .then((s) => setEngine(s.engine))
      .catch(() => setEngine(null))
  }, [])

  const badge = engine
    ? (STATUS_LABELS[engine.status] ?? { label: engine.status, tone: 'bg-slate-100 text-slate-600' })
    : null

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-8 py-4">
        <div>
          <h1 className="text-xl font-semibold">Job Agent</h1>
          <p className="text-sm text-slate-500">
            LinkedIn job search with tailored resume + cover letter packages
          </p>
        </div>
        {badge && (
          <span
            className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}
            title={engine?.last_error ?? undefined}
          >
            {badge.label}
            {engine?.model ? ` · ${engine.model}` : ''}
          </span>
        )}
      </header>
      <nav className="border-b border-slate-200 bg-white px-8">
        <div className="mx-auto flex max-w-7xl gap-6">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`border-b-2 py-3 text-sm font-medium ${
                tab === t.id
                  ? 'border-slate-900 text-slate-900'
                  : 'border-transparent text-slate-500 hover:text-slate-800'
              }`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </nav>
      <main className="mx-auto max-w-7xl p-8">
        {tab === 'profile' ? <Onboarding /> : <Search />}
      </main>
    </div>
  )
}
