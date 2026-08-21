import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { Education, Experience, Profile, ProfilePayload, Project, SkillGroup } from '../api'

// Blank entries so the user can add anything extraction missed — the
// confirmation editor must allow correcting *every* field, including
// whole entries that were omitted.
const EMPTY_EXPERIENCE: Experience = {
  title: '',
  company: '',
  location: null,
  start: '',
  end: '',
  bullets: [],
}
const EMPTY_PROJECT: Project = {
  name: '',
  technologies: [],
  date: null,
  link: null,
  bullets: [],
}
const EMPTY_EDUCATION: Education = {
  institution: '',
  location: null,
  degree: '',
  start: null,
  end: null,
  details: [],
}
const EMPTY_SKILL_GROUP: SkillGroup = { category: '', items: [] }

const inputCls =
  'w-full rounded-md border border-slate-300 px-2 py-1 text-sm focus:border-slate-500 focus:outline-none'
const labelCls = 'block text-xs font-medium uppercase tracking-wide text-slate-500'

function Field({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="block">
      <span className={labelCls}>{label}</span>
      <input className={inputCls} value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  )
}

function BulletsEditor({
  bullets,
  onChange,
  addLabel = 'bullet',
}: {
  bullets: string[]
  onChange: (b: string[]) => void
  addLabel?: string
}) {
  return (
    <div className="space-y-1">
      {bullets.map((bullet, i) => (
        <div key={i} className="flex gap-1">
          <textarea
            className={`${inputCls} min-h-[3.5rem]`}
            value={bullet}
            onChange={(e) => onChange(bullets.map((b, j) => (j === i ? e.target.value : b)))}
          />
          <button
            className="self-start text-slate-400 hover:text-rose-600"
            title={`Remove ${addLabel}`}
            onClick={() => onChange(bullets.filter((_, j) => j !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        className="text-sm text-slate-500 hover:text-slate-800"
        onClick={() => onChange([...bullets, ''])}
      >
        + {addLabel}
      </button>
    </div>
  )
}

function SectionHeader({ title, onAdd, addLabel }: { title: string; onAdd: () => void; addLabel: string }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h3 className="font-medium">{title}</h3>
      <button className="text-sm text-slate-500 hover:text-slate-800" onClick={onAdd}>
        + {addLabel}
      </button>
    </div>
  )
}

function RemoveEntryButton({ title, onRemove }: { title: string; onRemove: () => void }) {
  return (
    <button
      className="absolute right-2 top-2 text-slate-400 hover:text-rose-600"
      title={title}
      onClick={onRemove}
    >
      ✕
    </button>
  )
}

export default function Onboarding() {
  const [payload, setPayload] = useState<ProfilePayload | null>(null)
  const [draft, setDraft] = useState<Profile | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    api
      .getProfile()
      .then((p) => {
        setPayload(p)
        if (p.profile) setDraft(structuredClone(p.profile))
      })
      .catch((e) => setError(String(e)))
  }, [])

  const upload = useCallback(async (file: File) => {
    setBusy('Extracting your resume with Claude — this takes a minute…')
    setError(null)
    try {
      const p = await api.uploadResume(file)
      setPayload(p)
      setDraft(structuredClone(p.profile!))
      setDirty(false)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [])

  const save = useCallback(async () => {
    if (!draft) return
    setBusy('Re-rendering preview…')
    setError(null)
    try {
      const p = await api.saveProfile(draft)
      setPayload(p)
      setDirty(false)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [draft])

  const confirm = useCallback(async () => {
    if (!payload?.version) return
    setBusy('Confirming…')
    try {
      await api.confirmProfile(payload.version)
      setPayload({ ...payload, confirmed: true })
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [payload])

  const edit = (updater: (p: Profile) => void) => {
    if (!draft) return
    const next = structuredClone(draft)
    updater(next)
    setDraft(next)
    setDirty(true)
  }

  if (!payload) {
    return <p className="text-slate-400">{error ?? 'Loading…'}</p>
  }

  if (!payload.version || !draft) {
    return (
      <section className="rounded-xl border border-slate-200 bg-white p-8 text-center shadow-sm">
        <h2 className="mb-2 text-lg font-semibold">Upload your resume</h2>
        <p className="mb-6 text-sm text-slate-500">
          PDF or DOCX. It will be re-rendered into a clean, ATS-friendly template that you can
          correct and confirm. Content is sent to Claude for extraction.
        </p>
        <label className="inline-block cursor-pointer rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-700">
          {busy ?? 'Choose file'}
          <input
            type="file"
            accept=".pdf,.docx"
            className="hidden"
            disabled={busy !== null}
            onChange={(e) => {
              const file = e.target.files?.[0]
              // Reset the input as soon as the file is captured: a failed
              // upload (usage limit, unsupported file) leaves the same
              // file selected, and re-picking it fires no change event —
              // the retry would look dead. An empty value makes the next
              // pick a real change, same file or not.
              e.target.value = ''
              if (file) upload(file)
            }}
          />
        </label>
        {error && <p className="mt-4 text-sm text-rose-600">{error}</p>}
      </section>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <section className="space-y-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Your profile</h2>
          <div className="flex items-center gap-2">
            {payload.confirmed && !dirty ? (
              <span className="rounded-full bg-emerald-100 px-3 py-1 text-sm font-medium text-emerald-800">
                Confirmed
              </span>
            ) : (
              <>
                <button
                  className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-40"
                  disabled={!dirty || busy !== null}
                  onClick={save}
                >
                  Save & re-render
                </button>
                <button
                  className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
                  disabled={dirty || busy !== null}
                  onClick={confirm}
                >
                  Looks right — confirm
                </button>
              </>
            )}
          </div>
        </div>
        {busy && <p className="text-sm text-slate-500">{busy}</p>}
        {error && <p className="text-sm text-rose-600">{error}</p>}

        <Field label="Name" value={draft.name} onChange={(v) => edit((p) => (p.name = v))} />
        <Field
          label="Headline"
          value={draft.headline ?? ''}
          onChange={(v) => edit((p) => (p.headline = v || null))}
        />

        <div className="grid grid-cols-2 gap-3">
          {(['phone', 'email', 'linkedin', 'github', 'website', 'location'] as const).map(
            (key) => (
              <Field
                key={key}
                label={key}
                value={draft.contact[key] ?? ''}
                onChange={(v) => edit((p) => (p.contact[key] = v || null))}
              />
            ),
          )}
        </div>

        <div>
          <SectionHeader
            title="Experience"
            addLabel="experience"
            onAdd={() => edit((p) => p.experiences.push(structuredClone(EMPTY_EXPERIENCE)))}
          />
          {draft.experiences.map((exp, i) => (
            <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-slate-200 p-3 pr-8">
              <RemoveEntryButton title="Remove experience" onRemove={() => edit((p) => p.experiences.splice(i, 1))} />
              <div className="grid grid-cols-2 gap-2">
                <Field label="Title" value={exp.title} onChange={(v) => edit((p) => (p.experiences[i].title = v))} />
                <Field label="Company" value={exp.company} onChange={(v) => edit((p) => (p.experiences[i].company = v))} />
                <Field label="Location" value={exp.location ?? ''} onChange={(v) => edit((p) => (p.experiences[i].location = v || null))} />
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Start" value={exp.start} onChange={(v) => edit((p) => (p.experiences[i].start = v))} />
                  <Field label="End" value={exp.end} onChange={(v) => edit((p) => (p.experiences[i].end = v))} />
                </div>
              </div>
              <BulletsEditor bullets={exp.bullets} onChange={(b) => edit((p) => (p.experiences[i].bullets = b))} />
            </div>
          ))}
        </div>

        <div>
          <SectionHeader
            title="Projects"
            addLabel="project"
            onAdd={() => edit((p) => p.projects.push(structuredClone(EMPTY_PROJECT)))}
          />
          {draft.projects.map((proj, i) => (
            <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-slate-200 p-3 pr-8">
              <RemoveEntryButton title="Remove project" onRemove={() => edit((p) => p.projects.splice(i, 1))} />
              <div className="grid grid-cols-2 gap-2">
                <Field label="Name" value={proj.name} onChange={(v) => edit((p) => (p.projects[i].name = v))} />
                <Field label="Date" value={proj.date ?? ''} onChange={(v) => edit((p) => (p.projects[i].date = v || null))} />
                <Field
                  label="Technologies (comma-separated)"
                  value={proj.technologies.join(', ')}
                  onChange={(v) =>
                    edit((p) => (p.projects[i].technologies = v.split(',').map((s) => s.trim()).filter(Boolean)))
                  }
                />
                <Field label="Link" value={proj.link ?? ''} onChange={(v) => edit((p) => (p.projects[i].link = v || null))} />
              </div>
              <BulletsEditor bullets={proj.bullets} onChange={(b) => edit((p) => (p.projects[i].bullets = b))} />
            </div>
          ))}
        </div>

        <div>
          <SectionHeader
            title="Education"
            addLabel="education"
            onAdd={() => edit((p) => p.education.push(structuredClone(EMPTY_EDUCATION)))}
          />
          {draft.education.map((edu, i) => (
            <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-slate-200 p-3 pr-8">
              <RemoveEntryButton title="Remove education" onRemove={() => edit((p) => p.education.splice(i, 1))} />
              <div className="grid grid-cols-2 gap-2">
                <Field label="Institution" value={edu.institution} onChange={(v) => edit((p) => (p.education[i].institution = v))} />
                <Field label="Degree" value={edu.degree} onChange={(v) => edit((p) => (p.education[i].degree = v))} />
                <Field label="Location" value={edu.location ?? ''} onChange={(v) => edit((p) => (p.education[i].location = v || null))} />
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Start" value={edu.start ?? ''} onChange={(v) => edit((p) => (p.education[i].start = v || null))} />
                  <Field label="End" value={edu.end ?? ''} onChange={(v) => edit((p) => (p.education[i].end = v || null))} />
                </div>
              </div>
              <BulletsEditor
                bullets={edu.details}
                addLabel="detail"
                onChange={(d) => edit((p) => (p.education[i].details = d))}
              />
            </div>
          ))}
        </div>

        <div>
          <SectionHeader
            title="Skills"
            addLabel="skill group"
            onAdd={() => edit((p) => p.skills.push(structuredClone(EMPTY_SKILL_GROUP)))}
          />
          {draft.skills.map((group, i) => (
            <div key={i} className="mb-2 flex items-start gap-2">
              <div className="grid flex-1 grid-cols-3 gap-2">
                <Field label="Category" value={group.category} onChange={(v) => edit((p) => (p.skills[i].category = v))} />
                <div className="col-span-2">
                  <Field
                    label="Items (comma-separated)"
                    value={group.items.join(', ')}
                    onChange={(v) =>
                      edit((p) => (p.skills[i].items = v.split(',').map((s) => s.trim()).filter(Boolean)))
                    }
                  />
                </div>
              </div>
              <button
                className="mt-5 text-slate-400 hover:text-rose-600"
                title="Remove skill group"
                onClick={() => edit((p) => p.skills.splice(i, 1))}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-2 shadow-sm">
        <iframe
          key={payload.version}
          title="Resume preview"
          src={`/api/profile/preview.pdf?version=${payload.version}&inline=1`}
          className="h-[85vh] w-full rounded-lg"
        />
      </section>
    </div>
  )
}
