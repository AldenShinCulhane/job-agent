import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { Job, LinkedInStatus, SearchRun } from '../api'

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

const WORKPLACE_OPTIONS = ['remote', 'hybrid', 'onsite'] as const

const EXPERIENCE_OPTIONS = [
  { id: 'internship', label: 'Internship' },
  { id: 'entry', label: 'Entry level' },
  { id: 'associate', label: 'Associate' },
  { id: 'mid_senior', label: 'Mid-Senior' },
  { id: 'director', label: 'Director' },
  { id: 'executive', label: 'Executive' },
] as const

export default function Search() {
  const [linkedin, setLinkedin] = useState<LinkedInStatus | null>(null)
  const [run, setRun] = useState<SearchRun | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busyDetail, setBusyDetail] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [keywords, setKeywords] = useState('')
  const [location, setLocation] = useState('')
  const [datePosted, setDatePosted] = useState('')
  const [workplace, setWorkplace] = useState<string[]>([])
  const [experience, setExperience] = useState<string[]>([])
  const [cap, setCap] = useState(100)

  const refresh = useCallback(async () => {
    try {
      const [status, latest, jobList] = await Promise.all([
        api.linkedinStatus(),
        api.searchLatest(),
        api.jobs(),
      ])
      setLinkedin(status)
      setRun(latest.id === null ? null : latest)
      setJobs(jobList.jobs)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  // Fast poll while a run is active or the session is mid-transition;
  // slow poll while a stop will self-resume (cap reset, auth/captcha
  // recovery) so the UI catches the backend's automatic restart.
  const fastPoll =
    run?.status === 'running' ||
    linkedin?.state === 'connecting' ||
    linkedin?.state === 'logged_out' ||
    linkedin?.state === 'captcha_stop'
  const slowPoll =
    !fastPoll &&
    (run?.status === 'stopped_cap' ||
      run?.status === 'stopped_auth' ||
      run?.status === 'stopped_captcha')
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
          Automated collection is paced but still against LinkedIn's terms — account
          restriction is a risk you accept (see README).
        </p>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-lg font-semibold">Job search</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <label className="col-span-2 block">
            <span className={labelCls}>Keywords</span>
            <input className={inputCls} value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="software engineer" />
          </label>
          <label className="block">
            <span className={labelCls}>Location</span>
            <input className={inputCls} value={location} onChange={(e) => setLocation(e.target.value)} placeholder="Toronto, ON" />
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
        <h2 className="mb-4 text-lg font-semibold">Jobs ({jobs.length})</h2>
        {jobs.length === 0 ? (
          <p className="text-sm text-slate-400">No jobs collected yet.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {jobs.map((job) => (
              <li key={job.job_id} className="py-3">
                <div className="flex items-start justify-between gap-4">
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
                  <div className="flex shrink-0 items-center gap-2">
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
                          ? 'Hide description'
                          : 'Description'}
                    </button>
                  </div>
                </div>
                {expanded === job.job_id && job.description && (
                  <p className="mt-2 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                    {job.description}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
