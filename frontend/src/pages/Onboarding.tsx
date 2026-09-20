import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Education, Experience, Profile, ProfilePayload, Project, SkillGroup } from '../api'
import { useEpochEffect } from '../useEventStream'
import { useFocusLanding } from '../focus'
import { useLatestOnly } from '../useLatestOnly'
import { btnPrimary, btnSecondary, chip, inputCls, labelCls } from '../styles'
import type { ReadOptions } from '../useLatestOnly'

// Blank entries so the user can add anything extraction missed — the
// confirmation editor must allow correcting *every* field, including
// whole entries that were omitted.
// How often a refresh that is owed but has not landed tries again.
const RETRY_STALE_MS = 5_000

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
            aria-label={`${addLabel} ${i + 1}`}
            value={bullet}
            onChange={(e) => onChange(bullets.map((b, j) => (j === i ? e.target.value : b)))}
          />
          <button
            className="self-start text-ink-3 hover:text-danger-fg"
            title={`Remove ${addLabel}`}
            onClick={() => onChange(bullets.filter((_, j) => j !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        className="text-sm text-ink-3 hover:text-ink"
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
      <button className="text-sm text-ink-3 hover:text-ink" onClick={onAdd}>
        + {addLabel}
      </button>
    </div>
  )
}

function RemoveEntryButton({ title, onRemove }: { title: string; onRemove: () => void }) {
  return (
    <button
      className="absolute right-2 top-2 text-ink-3 hover:text-danger-fg"
      title={title}
      onClick={onRemove}
    >
      ✕
    </button>
  )
}

/** The one file-picker control. Both the first-run empty state and the
 * "Replace resume" panel mount this: a second copy of the reset-on-capture
 * logic below is exactly how the retry-looks-dead bug comes back. */
function ResumePicker({
  label,
  busy,
  onPick,
}: {
  label: string
  busy: string | null
  onPick: (file: File) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <>
      <button
        type="button"
        className={`${btnPrimary} inline-block rounded-lg px-5 py-2.5 text-sm font-medium`}
        disabled={busy !== null}
        onClick={() => inputRef.current?.click()}
      >
        {busy ?? label}
      </button>
      <input
        ref={inputRef}
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
          if (file) onPick(file)
        }}
      />
    </>
  )
}

export default function Onboarding({ profileEpoch }: { profileEpoch: number }) {
  const [payload, setPayload] = useState<ProfilePayload | null>(null)
  const [draft, setDraft] = useState<Profile | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [replacing, setReplacing] = useState(false)
  // Bumped by every local change to the editor: a keystroke, or the start
  // of an action. A refetch is only safe to apply if none of those landed
  // while it was in flight — checking `dirty` before the request is not
  // enough, because the user can start typing during it.
  const localSeq = useRef(0)
  // Where focus goes when an action replaces the control that had it:
  // confirm swaps the buttons for a chip, discard and upload replace
  // the whole editor.
  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const land = useFocusLanding()

  // Invalidations are COUNTED, not flagged. A second profile event
  // arriving while the first is still being served has to be
  // distinguishable, or a response fetched for v5 would acknowledge the
  // v6 announcement that overtook it and v6 would never be read at all.
  // `owed` is then "the newest generation has not been adopted yet", and
  // a request may only acknowledge the generation it was issued for.
  const invalidationsRef = useRef(0)
  const [invalidations, setInvalidations] = useState(0)
  const [acknowledged, setAcknowledged] = useState(0)
  const owed = acknowledged < invalidations
  const invalidate = useCallback(() => {
    invalidationsRef.current += 1
    setInvalidations(invalidationsRef.current)
  }, [])

  const readProfile = useLatestOnly()

  /** Reads the profile, adopts it when it is still adoptable, and
   * acknowledges the invalidation generation it was ISSUED for — never a
   * later one. Resolves whether it adopted, which is not the same thing:
   * a response can be adopted and still leave the page owed a read,
   * because an event overtook it. */
  const load = useCallback((options?: ReadOptions) => {
    const seq = localSeq.current
    const generation = invalidationsRef.current
    let adopted = false
    return readProfile(
      () => api.getProfile(),
      (p) => {
        // A response describing a profile the user has since moved away
        // from is not adoptable; leaving it un-adopted keeps the
        // invalidation armed for when the editor is clean and idle.
        if (localSeq.current !== seq) return
        adopted = true
        setPayload(p)
        if (p.profile) setDraft(structuredClone(p.profile))
      },
      options,
    )
      .then(() => {
        // Only this request's own generation is settled by it. Anything
        // announced while it was in flight describes a profile this
        // response cannot contain.
        if (adopted && invalidationsRef.current === generation) {
          setAcknowledged((prev) => Math.max(prev, generation))
        }
        return adopted
      })
      .catch((e) => {
        setError(String(e))
        return false
      })
  }, [readProfile])

  useEffect(() => {
    // A failed first read leaves `payload` null and the page on
    // "Loading…" — arm the same reconciliation the event path uses
    // rather than waiting for a remount. A read that adopted but could
    // not acknowledge is already covered by the event that overtook it.
    void load().then((adopted) => {
      if (!adopted) invalidate()
    })
  }, [load, invalidate])

  // The profile moved on the server — another tab confirmed, discarded,
  // or re-uploaded. Adopting it right now would overwrite unsaved edits
  // (load() replaces the draft), so the refetch is held until the editor
  // is clean and idle. A confirm that lost the race 409s on its own, so
  // waiting is safe as well as non-destructive.
  useEpochEffect(profileEpoch, invalidate)
  useEffect(() => {
    if (!owed || dirty || busy !== null) return
    // Settled by ACKNOWLEDGEMENT, never by merely having tried: a read
    // that failed leaves the editor showing data known to be stale, and
    // no further event is coming to prompt another attempt. `load`
    // acknowledges its own generation, which clears `owed` and tears
    // this down; the interval only ever fires while that has not
    // happened.
    //
    // Two kinds of attempt. Entering here is PROMPTED — every dependency
    // is a real change: a new announcement (`invalidations`, so it
    // re-enters at once rather than waiting out the interval), or the
    // editor just became clean and idle. That read supersedes whatever
    // is in flight: the pending answer is behind the announcement, or
    // was issued before the edit and cannot be adopted anyway. A TICK is
    // not a change, so it is `ifIdle`: while the last read is still out
    // it issues nothing. A tick that superseded would, with a round trip
    // longer than the interval, cancel every response before it landed
    // — nothing adopted, nothing acknowledged, retrying forever.
    void load()
    const timer = setInterval(() => void load({ ifIdle: true }), RETRY_STALE_MS)
    return () => clearInterval(timer)
  }, [owed, invalidations, dirty, busy, load])

  const upload = useCallback(async (file: File) => {
    localSeq.current += 1
    setBusy('Extracting your resume with Claude — this takes a minute…')
    setError(null)
    try {
      const p = await api.uploadResume(file)
      setPayload(p)
      setDraft(structuredClone(p.profile!))
      setDirty(false)
      setReplacing(false)
      land(headingRef)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [land])

  const save = useCallback(async () => {
    if (!draft) return
    const seq = (localSeq.current += 1)
    setBusy('Re-rendering preview…')
    setError(null)
    try {
      const p = await api.saveProfile(draft)
      setPayload(p)
      // Clearing `dirty` is a claim that the editor matches the version
      // just written. If anything edited it after this request went out,
      // that claim is false — and a background refetch would then sail
      // through the clean-and-idle guard and overwrite the newer text
      // with what was saved.
      if (localSeq.current === seq) setDirty(false)
      land(headingRef)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [draft, land])

  const discard = useCallback(async () => {
    localSeq.current += 1
    setBusy('Discarding draft…')
    setError(null)
    try {
      const p = await api.discardDraft()
      setPayload(p)
      setDraft(structuredClone(p.profile!))
      setDirty(false)
      setReplacing(false)
      land(headingRef)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [land])

  const confirm = useCallback(async () => {
    if (!payload?.version) return
    localSeq.current += 1
    setBusy('Confirming…')
    try {
      setPayload(await api.confirmProfile(payload.version))
      land(headingRef)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }, [payload, land])

  const edit = (updater: (p: Profile) => void) => {
    if (!draft) return
    const next = structuredClone(draft)
    updater(next)
    localSeq.current += 1
    setDraft(next)
    setDirty(true)
  }

  if (!payload) {
    return <p className="text-ink-3">{error ?? 'Loading…'}</p>
  }

  if (!payload.version || !draft) {
    return (
      <section className="rounded-xl border border-line bg-surface p-8 text-center shadow-card">
        <h2 className="mb-2 text-lg font-semibold">Upload your resume</h2>
        <p className="mb-6 text-sm text-ink-3">
          PDF or DOCX. It will be re-rendered into a clean, ATS-friendly template that you can
          correct and confirm. Content is sent to Claude for extraction.
        </p>
        <ResumePicker label="Choose file" busy={busy} onPick={upload} />
        {error && <p className="mt-4 text-sm text-danger-fg">{error}</p>}
      </section>
    )
  }

  // Newest confirmed version, which is not necessarily what is on screen:
  // an upload or an edit puts a newer, unconfirmed row on top, and the
  // pipeline is then paused rather than running on either one.
  const live = payload.live_version ?? null
  const previewUrl = `/api/profile/preview.pdf?version=${payload.version}&inline=1`

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <section className="space-y-6 rounded-xl border border-line bg-surface p-6 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 ref={headingRef} tabIndex={-1} className="text-lg font-semibold">
            Your profile
            <span className="ml-2 text-sm font-normal text-ink-3">
              {dirty
                ? `Unsaved edits${live !== null ? ` · live is v${live}` : ''}`
                : payload.confirmed
                  ? `Live v${payload.version}`
                  : `Draft v${payload.version} · not live${live !== null ? ` (v${live} is)` : ''}`}
            </span>
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <button
              className={`${btnSecondary} rounded-md px-3 py-1.5 text-sm`}
              disabled={busy !== null}
              onClick={() => setReplacing((v) => !v)}
            >
              {replacing ? 'Cancel' : 'Replace resume'}
            </button>
            {payload.confirmed && !dirty ? (
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${chip.ok}`}>
                Confirmed
              </span>
            ) : (
              <>
                <button
                  className={`${btnSecondary} rounded-md px-3 py-1.5 text-sm`}
                  disabled={!dirty || busy !== null}
                  onClick={save}
                >
                  Save & re-render
                </button>
                <button
                  className={`${btnPrimary} rounded-md px-3 py-1.5 text-sm font-medium`}
                  disabled={dirty || busy !== null}
                  onClick={confirm}
                >
                  Looks right — confirm
                </button>
                {live !== null && (
                  <button
                    className={`${btnSecondary} rounded-md px-3 py-1.5 text-sm text-ink-2`}
                    disabled={busy !== null}
                    onClick={discard}
                    title={`Throw away this draft and go back to live v${live}`}
                  >
                    Discard draft
                  </button>
                )}
              </>
            )}
          </div>
        </div>
        {!payload.confirmed && (
          <p className="rounded-lg bg-surface-2 px-3 py-2 text-sm text-ink-2">
            This draft is not live. Scoring is paused and no new packages can be
            started until you confirm it
            {live !== null ? ' — or discard it to go back to v' + live : ''}. A
            package already building keeps going on the version it started from.
          </p>
        )}
        {busy && <p className="text-sm text-ink-3">{busy}</p>}
        {error && <p className="text-sm text-danger-fg">{error}</p>}
        {replacing && (
          <div className="space-y-3 rounded-lg border border-line bg-warn-bg p-4">
            <p className="text-sm text-warn-fg">
              A new PDF or DOCX is extracted into a fresh profile version that replaces
              everything below. It is not live until you confirm it — scoring is paused
              and no new packages can be started in between — and confirming fails any
              package that was still building on the old version. Finished packages and
              their PDFs are history and are never touched.
            </p>
            <ResumePicker label="Choose a new resume" busy={busy} onPick={upload} />
          </div>
        )}

        {/* Every editable control at once. `disabled` propagates to
            descendants, so no edit can land while a mutation is in
            flight — which is the only window in which a response could
            arrive describing a profile the editor has already moved
            past. Preflight resets the fieldset's own border/padding. */}
        <fieldset disabled={busy !== null} className="min-w-0 space-y-6">
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
              <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-line p-3 pr-8">
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
              <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-line p-3 pr-8">
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
              <div key={i} className="relative mb-4 space-y-2 rounded-lg border border-line p-3 pr-8">
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
                  className="mt-5 text-ink-3 hover:text-danger-fg"
                  title="Remove skill group"
                  onClick={() => edit((p) => p.skills.splice(i, 1))}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </fieldset>
      </section>

      <section className="rounded-xl border border-line bg-surface p-2 shadow-card">
        <iframe
          key={payload.version}
          title="Resume preview"
          src={previewUrl}
          className="h-[85vh] w-full rounded-lg"
        />
        {/* The pane needs the browser's own PDF viewer; where that is
            off or the frame fails, this is the same file by another
            door. */}
        <p className="py-2 text-center text-sm">
          <a
            className="text-ink-2 underline hover:text-ink"
            href={previewUrl}
            target="_blank"
            rel="noreferrer"
          >
            Open the preview PDF in a new tab
          </a>
        </p>
      </section>
    </div>
  )
}
