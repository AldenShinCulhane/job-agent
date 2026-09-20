import test from 'node:test'
import assert from 'node:assert/strict'
import { badgeFor, chip } from './styles.ts'
import type { Badge } from './styles.ts'

const MAP: Record<string, Badge> = { ok: { label: 'Fine', tone: 'ok' } }

test('a known status gets its own badge', () => {
  assert.deepEqual(badgeFor(MAP, 'ok'), { label: 'Fine', tone: 'ok' })
})

test('an unknown status gets its raw name in the neutral tone', () => {
  const badge = badgeFor(MAP, 'rebooting')
  assert.deepEqual(badge, { label: 'rebooting', tone: 'neutral' })
  assert.equal(typeof chip[badge.tone], 'string')
})

test('prototype names are not statuses', () => {
  assert.deepEqual(badgeFor(MAP, 'constructor'), { label: 'constructor', tone: 'neutral' })
})
