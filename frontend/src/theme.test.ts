import test from 'node:test'
import assert from 'node:assert/strict'
import { readPreference } from './theme.ts'

function withStorage(storage: unknown, run: () => void) {
  const g = globalThis as { localStorage?: unknown }
  const before = g.localStorage
  g.localStorage = storage
  try {
    run()
  } finally {
    g.localStorage = before
  }
}

test('a stored override is read back', () => {
  withStorage({ getItem: () => 'dark' }, () => assert.equal(readPreference(), 'dark'))
  withStorage({ getItem: () => 'light' }, () => assert.equal(readPreference(), 'light'))
})

test('nothing stored, garbage stored, or storage that throws all mean System', () => {
  withStorage({ getItem: () => null }, () => assert.equal(readPreference(), 'system'))
  withStorage({ getItem: () => 'purple' }, () => assert.equal(readPreference(), 'system'))
  withStorage(
    { getItem: () => { throw new Error('blocked') } },
    () => assert.equal(readPreference(), 'system'),
  )
  withStorage(undefined, () => assert.equal(readPreference(), 'system'))
})
