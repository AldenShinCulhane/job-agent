#!/usr/bin/env node
// Contrast check for the design tokens in src/index.css.
//
// Reads every `--name: light-dark(#light, #dark)` declaration and checks the
// pairs that actually occur in the UI, in both schemes. Text pairs must reach
// WCAG AA 4.5:1; control boundaries and the focus indicator must reach 3:1
// against the surfaces they sit on. Decorative separators are reported but
// never fail. A token that is missing, or written in anything but 6-digit
// hex, fails the run: an unmeasured colour is not a passing colour.
//
// Usage: node scripts/contrast.mjs   (also `npm run check:contrast`)

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(join(here, '..', 'src', 'index.css'), 'utf8')

const TOKENS = [
  'bg', 'surface', 'surface-2', 'line', 'line-strong',
  'ink', 'ink-2', 'ink-3',
  'primary', 'primary-ink', 'primary-hover', 'accent',
  'ok-fg', 'ok-bg', 'warn-fg', 'warn-bg', 'danger-fg', 'danger-bg', 'info-fg', 'info-bg',
]

const AA_TEXT = 4.5
const AA_NON_TEXT = 3

// [foreground, background, minimum, what it is]. "info" pairs only report.
const PAIRS = [
  ...['ink', 'ink-2', 'ink-3'].flatMap((ink) =>
    ['bg', 'surface', 'surface-2'].map((s) => [ink, s, AA_TEXT, 'text'])),
  ...['ok', 'warn', 'danger', 'info'].flatMap((t) => [
    [`${t}-fg`, `${t}-bg`, AA_TEXT, 'chip text on its tint'],
    [`${t}-fg`, 'surface', AA_TEXT, 'note text on a card'],
    [`${t}-fg`, 'surface-2', AA_TEXT, 'note text on an inset panel'],
    [`${t}-fg`, 'bg', AA_TEXT, 'note text on the page'],
  ]),
  ['primary-ink', 'primary', AA_TEXT, 'button text'],
  ['primary-ink', 'primary-hover', AA_TEXT, 'button text, hovered'],
  ['accent', 'surface', AA_TEXT, 'accent as text'],
  ['accent', 'bg', AA_TEXT, 'accent as text'],
  ['accent', 'surface-2', AA_TEXT, 'accent as text'],
  ['line-strong', 'surface', AA_NON_TEXT, 'input boundary'],
  ['line-strong', 'surface-2', AA_NON_TEXT, 'input boundary on an inset panel'],
  ['line-strong', 'bg', AA_NON_TEXT, 'control boundary on the page'],
  ['accent', 'surface', AA_NON_TEXT, 'focus ring / checkbox'],
  ['accent', 'surface-2', AA_NON_TEXT, 'focus ring on an inset panel'],
  ['accent', 'bg', AA_NON_TEXT, 'focus ring on the page'],
  // Not listed: accent on primary. The ring has outline-offset: 2px, so the
  // colour adjacent to it on both sides is the surface it sits on, not the
  // button fill it surrounds. Those pairs are the three above.
  ['line', 'surface', 0, 'decorative separator (info only)'],
]

function parseTokens() {
  const out = {}
  const problems = []
  for (const name of TOKENS) {
    const re = new RegExp(`--${name}:\\s*light-dark\\(\\s*(#[0-9a-fA-F]{6})\\s*,\\s*(#[0-9a-fA-F]{6})\\s*\\)\\s*;`)
    const m = css.match(re)
    if (!m) {
      problems.push(`--${name}: missing, or not \`light-dark(#rrggbb, #rrggbb)\``)
      continue
    }
    out[name] = { light: m[1].toLowerCase(), dark: m[2].toLowerCase() }
  }
  return { tokens: out, problems }
}

function luminance(hex) {
  const channel = (c) => {
    const v = parseInt(c, 16) / 255
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  const r = channel(hex.slice(1, 3))
  const g = channel(hex.slice(3, 5))
  const b = channel(hex.slice(5, 7))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const { tokens, problems } = parseTokens()
let failures = problems.length
for (const p of problems) console.log(`FAIL  ${p}`)

for (const scheme of ['light', 'dark']) {
  console.log(`\n${scheme}`)
  for (const [fg, bg, min, what] of PAIRS) {
    if (!tokens[fg] || !tokens[bg]) continue // already reported as missing
    const ratio = contrast(tokens[fg][scheme], tokens[bg][scheme])
    const ok = min === 0 || ratio >= min
    if (!ok) failures += 1
    const mark = min === 0 ? 'info' : ok ? 'ok  ' : 'FAIL'
    console.log(
      `  ${mark}  ${ratio.toFixed(2).padStart(5)}  ${min ? `≥${min}` : '   '}  ${fg.padEnd(12)} on ${bg.padEnd(10)}  ${what}`,
    )
  }
}

console.log(failures ? `\n${failures} failure(s)` : '\nall pairs pass')
process.exit(failures ? 1 : 0)
