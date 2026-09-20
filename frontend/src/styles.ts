/** The TypeScript side of the design tokens in index.css.
 *
 * Colour only. Every string here is a complete literal class list on
 * purpose: Tailwind finds classes by scanning the source for literals, so a
 * name assembled from a tone at runtime (`bg-${tone}-bg`) is never
 * generated and renders as nothing. Geometry (padding, radius, type size)
 * stays at each call site — it is the same as it was, and it is P3's job.
 */

export type Tone = 'neutral' | 'ok' | 'info' | 'warn' | 'danger'

/** What a status renders as: its wording and which tone map entry. */
export interface Badge {
  label: string
  tone: Tone
}

/** The badge for a status, known or not. A status the map has no entry
 * for still gets one: its raw name, in the neutral tone. Every map goes
 * through this so the fallback is written once and type-checked once —
 * a fallback literal written at a call site after `??` is not checked
 * at all, because the index type says the lookup is never undefined and
 * TypeScript then drops the right operand's type. */
export function badgeFor(map: Record<string, Badge>, status: string): Badge {
  // Own entries only: a status named like an Object.prototype member
  // ('constructor') must not resolve to a function.
  return Object.hasOwn(map, status)
    ? (map[status] as Badge)
    : { label: status, tone: 'neutral' }
}

/** A pill or badge: tinted background with its matching text. */
export const chip: Record<Tone, string> = {
  neutral: 'bg-surface-2 text-ink-2',
  ok: 'bg-ok-bg text-ok-fg',
  info: 'bg-info-bg text-info-fg',
  warn: 'bg-warn-bg text-warn-fg',
  danger: 'bg-danger-bg text-danger-fg',
}

/** A full-width notice: the same tint, a hairline border. */
export const banner: Record<Tone, string> = {
  neutral: 'border-line bg-surface-2 text-ink-2',
  ok: 'border-line bg-ok-bg text-ok-fg',
  info: 'border-line bg-info-bg text-info-fg',
  warn: 'border-line bg-warn-bg text-warn-fg',
  danger: 'border-line bg-danger-bg text-danger-fg',
}

/** Coloured text on a neutral surface: an error line, a warning note. */
export const note: Record<Tone, string> = {
  neutral: 'text-ink-2',
  ok: 'text-ok-fg',
  info: 'text-info-fg',
  warn: 'text-warn-fg',
  danger: 'text-danger-fg',
}

/** The filled button. Neutral ink on paper; the accent is not a fill. */
export const btnPrimary =
  'bg-primary text-primary-ink hover:bg-primary-hover disabled:opacity-40'

/** The outlined button. Inherits the surrounding text colour. */
export const btnSecondary =
  'border border-line-strong hover:bg-surface-2 disabled:opacity-40'

/** Text inputs and selects. Keyboard focus comes from the global
 * `:focus-visible` rule, so nothing here suppresses the outline. */
export const inputCls =
  'w-full rounded-md border border-line-strong bg-surface px-2 py-1 text-sm text-ink'

export const labelCls = 'block text-xs font-medium uppercase tracking-wide text-ink-3'
