import { useCallback, useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, packageFileUrl } from '../api'
import type { Explanation, Job, LinkedInStatus, Package, SearchRun } from '../api'

const inputCls =
  'w-full rounded-md border border-slate-300 px-2 py-1 text-sm focus:border-slate-500 focus:outline-none'
const labelCls = 'block text-xs font-medium uppercase tracking-wide text-slate-500'

const LINKEDIN_STATES: Record<string, { label: string; tone: string }> = {
  idle: { label: 'Not connected', tone: 'bg-slate-100 text-slate-600' },
  connecting: { label: 'Connecting…', tone: 'bg-sky-100 text-sky-800' },
  ready: { label: 'LinkedIn connected', tone: 'bg-emerald-100 text-emerald-800' },
  logged_out: { label: 'Sign-in needed', tone: 'bg-amber-100 text-amber-800' },
  captcha_stop: { label: 'Checkpoint — stopped', tone: 'bg-rose-100 text-rose-800' },
  error: { label: 'Browser error', tone: 'bg-rose-100 text-rose-800' },
}

const RUN_LABELS: Record<string, string> = {
  running: 'Searching…',
  done: 'Search complete',
  stopped_captcha: 'Stopped at a LinkedIn checkpoint — resumes after you resolve it and reconnect',
  stopped_auth: 'Stopped — sign in via Connect and the search resumes automatically',
  stopped_cap: 'Daily action cap reached — resumes automatically when it resets',
  stopped_error: 'Stopped on an error',
  superseded: 'Replaced by a newer search',
}

const PACKAGE_STATES: Record<string, { label: string; tone: string }> = {
  queued: { label: 'Queued', tone: 'bg-slate-100 text-slate-600' },
  generating: { label: 'Generating…', tone: 'bg-sky-100 text-sky-800' },
  validating: { label: 'Validating…', tone: 'bg-sky-100 text-sky-800' },
  fitting: { label: 'Fitting pages…', tone: 'bg-sky-100 text-sky-800' },
  asserting: { label: 'Final checks…', tone: 'bg-sky-100 text-sky-800' },
  publishing: { label: 'Publishing…', tone: 'bg-sky-100 text-sky-800' },
  done: { label: 'Ready', tone: 'bg-emerald-100 text-emerald-800' },
  failed: { label: 'Failed', tone: 'bg-rose-100 text-rose-800' },
  waiting_usage: {
    label: 'Waiting — usage limit',
    tone: 'bg-amber-100 text-amber-800',
  },
}

const PACKAGE_ACTIVE = new Set([
  'queued', 'generating', 'validating', 'fitting', 'asserting', 'publishing',
])

const WORKPLACE_OPTIONS = ['remote', 'hybrid', 'onsite'] as const

const EXPERIENCE_OPTIONS = [
  { id: 'internship', label: 'Internship' },
  { id: 'entry', label: 'Entry level' },
  { id: 'associate', label: 'Associate' },
  { id: 'mid_senior', label: 'Mid-Senior' },
  { id: 'director', label: 'Director' },
  { id: 'executive', label: 'Executive' },
] as const

// Tab on an empty field accepts its placeholder suggestion; focus stays
// put so the accepted value is visible, and the next Tab moves on.
const acceptPlaceholderOnTab =
  (setter: (value: string) => void) => (e: KeyboardEvent<HTMLInputElement>) => {
    const el = e.currentTarget
    if (e.key === 'Tab' && !e.shiftKey && el.value === '' && el.placeholder) {
      e.preventDefault()
      setter(el.placeholder)
    }
  }

export default function Search() {
  const [linkedin, setLinkedin] = useState<LinkedInStatus | null>(null)
  const [run, setRun] = useState<SearchRun | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [scoringPending, setScoringPending] = useState(0)
  const [scoringStall, setScoringStall] = useState<{
    reason: string
    detail: string | null
  } | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busyDetail, setBusyDetail] = useState<string | null>(null)
  const [busyExplain, setBusyExplain] = useState<string | null>(null)
  const [busyRerank, setBusyRerank] = useState(false)
  const [explanations, setExplanations] = useState<Record<string, Explanation>>({})
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [packages, setPackages] = useState<Package[]>([])
  const [busyGenerate, setBusyGenerate] = useState(false)
  const [sortBy, setSortBy] = useState<'score' | 'date'>('score')
  const [hideExceeds, setHideExceeds] = useState(false)
  const [easyOnly, setEasyOnly] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [profileLocation, setProfileLocation] = useState<string | null>(null)

  const [keywords, setKeywords] = useState('')
  const [location, setLocation] = useState('')
  const [datePosted, setDatePosted] = useState('')
  const [workplace, setWorkplace] = useState<string[]>([])
  const [experience, setExperience] = useState<string[]>([])
  const [cap, setCap] = useState(100)

  const refresh = useCallback(async () => {
    try {
      const [status, latest, jobList, packageList] = await Promise.all([
        api.linkedinStatus(),
        api.searchLatest(),
        api.jobs(),
        api.packages(),
      ])
      setLinkedin(status)
      setRun(latest.id === null ? null : latest)
      setJobs(jobList.jobs)
      setScoringPending(jobList.scoring_pending ?? 0)
      setScoringStall(jobList.scoring_stall ?? null)
      setPackages(packageList.packages)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  // The location suggestion comes from the confirmed profile's contact
  // block; fetched once — the profile doesn't change on this page.
  useEffect(() => {
    api
      .getProfile()
      .then((p) => setProfileLocation(p.profile?.contact.location ?? null))
      .catch(() => {})
  }, [])

  // Fast poll while a run is active or the session is mid-transition;
  // slow poll while a stop will self-resume (cap reset, auth/captcha
  // recovery) so the UI catches the backend's automatic restart.
  const packagesActive = packages.some((p) => PACKAGE_ACTIVE.has(p.status))
  const fastPoll =
    run?.status === 'running' ||
    (run?.status === 'done' && scoringPending > 0 && !scoringStall) || // pipeline working
    packagesActive || // tailoring batch in flight
    linkedin?.state === 'connecting' ||
    linkedin?.state === 'logged_out' ||
    linkedin?.state === 'captcha_stop'
  const slowPoll =
    !fastPoll &&
    (run?.status === 'stopped_cap' ||
      run?.status === 'stopped_auth' ||
      run?.status === 'stopped_captcha' ||
      packages.some((p) => p.status === 'waiting_usage') || // resumes itself
      (scoringPending > 0 && scoringStall !== null)) // stalled — watch lazily
  useEffect(() => {
    if (!fastPoll && !slowPoll) return
    const timer = setInterval(refresh, fastPoll ? 2500 : 30000)
    return () => clearInterval(timer)
  }, [fastPoll, slowPoll, refresh])

  const connect = async () => {
    setError(null)
    try {
      setLinkedin(await api.linkedinConnect())
    } catch (e) {
      setError(String(e))
    }
  }

  const start = async () => {
    setError(null)
    // A new search is a new result set — selections and explanations
    // from the previous run don't carry over.
    setSelected(new Set())
    setExplanations({})
    setExpanded(null)
    try {
      await api.searchStart({
        keywords,
        location: location || null,
        date_posted: datePosted || null,
        workplace,
        experience,
        cap,
      })
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const resume = async () => {
    setError(null)
    try {
      await api.searchResume()
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const fetchDetail = async (jobId: string) => {
    setBusyDetail(jobId)
    setError(null)
    try {
      const detail = await api.jobDetail(jobId)
      setJobs((all) => all.map((j) => (j.job_id === jobId ? { ...j, ...detail } : j)))
      setExpanded(jobId)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyDetail(null)
    }
  }

  const explain = async (jobId: string) => {
    setBusyExplain(jobId)
    setError(null)
    try {
      const explanation = await api.jobExplain(jobId)
      setExplanations((all) => ({ ...all, [jobId]: explanation }))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyExplain(null)
    }
  }

  const rerank = async () => {
    setBusyRerank(true)
    setError(null)
    try {
      await api.searchRerank()
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyRerank(false)
    }
  }

  // Gate 3: generating packages IS the job-selection gate — everything
  // after this click runs unattended.
  const generatePackages = async () => {
    setBusyGenerate(true)
    setError(null)
    try {
      await api.packagesGenerate([...selected])
      setSelected(new Set())
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyGenerate(false)
    }
  }

  const toggleSelected = (jobId: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })

  // llm_rerank (when present) refines ordering within its band; the
  // deterministic score stays visible as the backbone.
  const displayScore = (j: Job) => j.llm_rerank ?? j.deterministic_score

  // Posting date when LinkedIn gave a parseable one; the card's
  // first-seen time is only the fallback.
  const postedTime = (j: Job) => {
    const posted = j.posted_text ? Date.parse(j.posted_text) : NaN
    return Number.isNaN(posted) ? Date.parse(j.first_seen_at) || 0 : posted
  }

  const visibleJobs = useMemo(() => {
    let list = jobs
    if (hideExceeds)
      list = list.filter((j) => !j.score_breakdown?.exceeds_experience)
    if (easyOnly) list = list.filter((j) => j.easy_apply)
    if (sortBy === 'score')
      list = [...list].sort((a, b) => {
        const byScore = (displayScore(b) ?? -1) - (displayScore(a) ?? -1)
        if (byScore !== 0) return byScore
        // Equal scores: the LLM's rank ordinal breaks the tie (an
        // all-equal rerank cluster would otherwise lose its order).
        return (a.llm_rank ?? Infinity) - (b.llm_rank ?? Infinity)
      })
    else list = [...list].sort((a, b) => postedTime(b) - postedTime(a))
    return list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, hideExceeds, easyOnly, sortBy])

  const scoredCount = jobs.filter((j) => j.deterministic_score !== null).length

  const badge = linkedin
    ? (LINKEDIN_STATES[linkedin.state] ?? { label: linkedin.state, tone: 'bg-slate-100 text-slate-600' })
    : null

  return (
    <div className="space-y-6">
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">LinkedIn</h2>
          <div className="flex items-center gap-3">
            {badge && (
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}>
                {badge.label}
              </span>
            )}
            {linkedin?.state !== 'ready' && (
              <button
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
                disabled={linkedin?.state === 'connecting'}
                onClick={connect}
              >
                Connect
              </button>
            )}
          </div>
        </div>
        {linkedin?.detail && <p className="mt-2 text-sm text-amber-700">{linkedin.detail}</p>}
        <p className="mt-2 text-xs text-slate-400">
          A real browser window opens for sign-in; your session stays on this machine.
          Sign in with your LinkedIn email and password — Google blocks its sign-in
          inside automated browsers, so "Continue with Google" won't work here.
          Automated collection is paced but still against LinkedIn's terms — account
          restriction is a risk you accept (see README).
        </p>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-lg font-semibold">Job search</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <label className="col-span-2 block">
            <span className={labelCls}>Keywords</span>
            <input
              className={inputCls}
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              onKeyDown={acceptPlaceholderOnTab(setKeywords)}
              placeholder="software engineer"
            />
          </label>
          <label className="block">
            <span className={labelCls}>Location</span>
            <input
              className={inputCls}
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              onKeyDown={acceptPlaceholderOnTab(setLocation)}
              placeholder={profileLocation ?? 'Toronto, ON'}
            />
          </label>
          <label className="block">
            <span className={labelCls}>Posted within</span>
            <select className={inputCls} value={datePosted} onChange={(e) => setDatePosted(e.target.value)}>
              <option value="">Any time</option>
              <option value="day">Past day</option>
              <option value="week">Past week</option>
              <option value="month">Past month</option>
            </select>
          </label>
          <div>
            <span className={labelCls}>Workplace</span>
            <div className="mt-1 flex gap-3">
              {WORKPLACE_OPTIONS.map((w) => (
                <label key={w} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={workplace.includes(w)}
                    onChange={(e) =>
                      setWorkplace((prev) => (e.target.checked ? [...prev, w] : prev.filter((x) => x !== w)))
                    }
                  />
                  {w}
                </label>
              ))}
            </div>
          </div>
          <label className="block">
            <span className={labelCls}>Max results</span>
            <input
              type="number"
              min={1}
              max={500}
              className={inputCls}
              value={cap}
              onChange={(e) => setCap(Number(e.target.value) || 100)}
            />
          </label>
          <div className="col-span-2 md:col-span-3">
            <span className={labelCls}>Experience level</span>
            <div className="mt-1 flex flex-wrap gap-3">
              {EXPERIENCE_OPTIONS.map((opt) => (
                <label key={opt.id} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={experience.includes(opt.id)}
                    onChange={(e) =>
                      setExperience((prev) =>
                        e.target.checked ? [...prev, opt.id] : prev.filter((x) => x !== opt.id),
                      )
                    }
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-3">
          <button
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
            disabled={!keywords.trim() || run?.status === 'running'}
            onClick={start}
          >
            Start search
          </button>
          {run?.status === 'stopped_error' && (
            <button
              className="rounded-md border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
              onClick={resume}
            >
              Resume
            </button>
          )}
          {run?.status && (
            <span className="text-sm text-slate-500">
              {RUN_LABELS[run.status] ?? run.status}
              {typeof run.collected === 'number' && ` · ${run.collected} collected`}
            </span>
          )}
        </div>
        {run?.message && <p className="mt-2 text-sm text-amber-700">{run.message}</p>}
        {error && <p className="mt-2 text-sm text-rose-600">{error}</p>}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">
            Jobs ({visibleJobs.length}
            {visibleJobs.length !== jobs.length ? ` of ${jobs.length}` : ''})
          </h2>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {scoringPending > 0 && !scoringStall && (
              <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-medium text-sky-800">
                Scoring… {jobs.length - scoringPending}/{jobs.length}
              </span>
            )}
            {scoringPending > 0 && scoringStall && (
              <>
                <span
                  className="rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-800"
                  title={scoringStall.detail ?? undefined}
                >
                  {scoringStall.reason === 'engine_blocked'
                    ? 'Scoring paused — Claude engine needs attention'
                    : 'Scoring paused — automatic retries exhausted'}
                </span>
                <button
                  className="rounded-md border border-amber-300 px-2 py-1 text-xs text-amber-800 hover:bg-amber-50"
                  onClick={async () => {
                    setError(null)
                    try {
                      await api.retryScoring()
                      await refresh()
                    } catch (e) {
                      setError(String(e))
                    }
                  }}
                >
                  Retry scoring
                </button>
              </>
            )}
            {selected.size > 0 && (
              <button
                className="rounded-full bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-40"
                disabled={busyGenerate}
                onClick={generatePackages}
                title="Tailored resume + cover letter PDFs for each selected job"
              >
                {busyGenerate
                  ? 'Queueing…'
                  : `Generate packages (${selected.size})`}
              </button>
            )}
            <label className="flex items-center gap-1">
              Sort
              <select
                className="rounded-md border border-slate-300 px-1 py-0.5 text-sm"
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as 'score' | 'date')}
              >
                <option value="score">Score</option>
                <option value="date">Newest</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={hideExceeds}
                onChange={(e) => setHideExceeds(e.target.checked)}
              />
              Hide over-experience
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={easyOnly}
                onChange={(e) => setEasyOnly(e.target.checked)}
              />
              Easy Apply only
            </label>
            {scoredCount >= 2 && scoringPending === 0 && (
              <button
                className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-40"
                disabled={busyRerank}
                onClick={rerank}
                title="LLM-order the jobs scored within a few points of the top"
              >
                {busyRerank ? 'Reranking…' : 'Rerank close matches'}
              </button>
            )}
          </div>
        </div>
        {jobs.length === 0 ? (
          <p className="text-sm text-slate-400">No jobs collected yet.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {visibleJobs.map((job) => {
              const score = displayScore(job)
              const breakdown = job.score_breakdown
              const explanation = explanations[job.job_id]
              return (
                <li key={job.job_id} className="py-3">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        className="mt-1.5"
                        checked={selected.has(job.job_id)}
                        onChange={() => toggleSelected(job.job_id)}
                      />
                      {score !== null && (
                        <span
                          className={`mt-0.5 w-12 shrink-0 rounded-md px-1.5 py-0.5 text-center text-sm font-semibold ${
                            score >= 75
                              ? 'bg-emerald-100 text-emerald-800'
                              : score >= 50
                                ? 'bg-amber-100 text-amber-800'
                                : 'bg-slate-100 text-slate-600'
                          }`}
                          title={
                            job.llm_rerank !== null
                              ? `LLM rerank ${job.llm_rerank} (deterministic ${job.deterministic_score})`
                              : `Deterministic score ${job.deterministic_score}`
                          }
                        >
                          {Math.round(score)}
                        </span>
                      )}
                      <div>
                        <a
                          href={job.url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-medium text-slate-900 hover:underline"
                        >
                          {job.title}
                        </a>
                        <p className="text-sm text-slate-500">
                          {job.company}
                          {job.location ? ` · ${job.location}` : ''}
                          {job.posted_text ? ` · ${job.posted_text}` : ''}
                        </p>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {breakdown?.exceeds_experience && (
                        <span
                          className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800"
                          title="Demands more experience than the profile shows"
                        >
                          Over-exp
                        </span>
                      )}
                      {job.easy_apply && (
                        <span className="rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-800">
                          Easy Apply
                        </span>
                      )}
                      <button
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-40"
                        disabled={busyDetail === job.job_id}
                        onClick={() =>
                          job.description && expanded !== job.job_id
                            ? setExpanded(job.job_id)
                            : expanded === job.job_id
                              ? setExpanded(null)
                              : fetchDetail(job.job_id)
                        }
                      >
                        {busyDetail === job.job_id
                          ? 'Fetching…'
                          : expanded === job.job_id
                            ? 'Hide'
                            : 'Details'}
                      </button>
                    </div>
                  </div>
                  {expanded === job.job_id && (
                    <div className="mt-2 space-y-2">
                      {breakdown && (
                        <div className="rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                          <p className="text-xs uppercase tracking-wide text-slate-400">
                            Skills {breakdown.skills} · Experience{' '}
                            {breakdown.experience} · Education {breakdown.education}
                          </p>
                          {breakdown.match_reasons.map((r) => (
                            <p key={r} className="mt-1 text-emerald-700">
                              ✓ {r}
                            </p>
                          ))}
                          {breakdown.gap_reasons.map((r) => (
                            <p key={r} className="mt-1 text-amber-700">
                              ✗ {r}
                            </p>
                          ))}
                          {job.deterministic_score !== null && (
                            <button
                              className="mt-2 rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-white disabled:opacity-40"
                              disabled={busyExplain === job.job_id}
                              onClick={() => explain(job.job_id)}
                            >
                              {busyExplain === job.job_id
                                ? 'Explaining…'
                                : explanation
                                  ? 'Refresh explanation'
                                  : 'Explain fit'}
                            </button>
                          )}
                          {explanation && (
                            <div className="mt-2 border-t border-slate-200 pt-2">
                              <p className="font-medium text-slate-800">
                                {explanation.pitch}
                              </p>
                              {explanation.strengths.map((s) => (
                                <p key={s} className="mt-1 text-emerald-700">
                                  + {s}
                                </p>
                              ))}
                              {explanation.gaps.map((g) => (
                                <p key={g} className="mt-1 text-amber-700">
                                  – {g}
                                </p>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {job.description && (
                        <p className="whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                          {job.description}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {packages.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-lg font-semibold">Packages</h2>
          <ul className="divide-y divide-slate-100">
            {packages.map((pkg) => {
              const state = PACKAGE_STATES[pkg.status] ?? {
                label: pkg.status,
                tone: 'bg-slate-100 text-slate-600',
              }
              return (
                <li
                  key={pkg.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium text-slate-800">
                      {pkg.title}{' '}
                      <span className="font-normal text-slate-500">
                        — {pkg.company}
                      </span>
                    </p>
                    {pkg.error && pkg.status !== 'done' && (
                      <p className="mt-0.5 text-xs text-rose-600">{pkg.error}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-3 text-sm">
                    <span
                      className={`rounded-full px-3 py-1 text-xs font-medium ${state.tone}`}
                    >
                      {state.label}
                    </span>
                    {pkg.status === 'done' && (
                      <>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'resume.pdf')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Resume
                        </a>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'cover_letter.pdf')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Cover letter
                        </a>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'job.md')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Posting
                        </a>
                      </>
                    )}
                    <a
                      className="text-slate-400 underline hover:text-slate-600"
                      href={pkg.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      LinkedIn
                    </a>
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </div>
  )
}
