import test from 'node:test'
import assert from 'node:assert/strict'
import { episodeShown, markEpisodeShown } from './shownEpisodes.ts'

function withSession(storage: unknown, run: () => void) {
  const g = globalThis as { sessionStorage?: unknown }
  const before = g.sessionStorage
  g.sessionStorage = storage
  try {
    run()
  } finally {
    g.sessionStorage = before
  }
}

test('an episode opens the picker once, across reloads of the tab', () => {
  const data = new Map<string, string>()
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  }
  withSession(storage, () => {
    assert.equal(episodeShown('stored-a'), false)
    markEpisodeShown('stored-a')
    assert.equal(episodeShown('stored-a'), true)
    assert.equal(episodeShown('stored-b'), false)
  })
  // A reload: the module's memory is new, the tab's storage is not.
  assert.deepEqual(JSON.parse(data.get('job-agent.engine-episodes-shown') ?? '[]'), ['stored-a'])
})

test('blocked storage still remembers for the life of the page', () => {
  const blocked = {
    getItem: () => {
      throw new Error('SecurityError')
    },
    setItem: () => {
      throw new Error('SecurityError')
    },
  }
  withSession(blocked, () => {
    assert.equal(episodeShown('blocked-a'), false)
    markEpisodeShown('blocked-a')
    assert.equal(episodeShown('blocked-a'), true)
  })
})

test('garbage in storage reads as nothing shown', () => {
  withSession({ getItem: () => '{"not": "a list"}', setItem: () => {} }, () => {
    assert.equal(episodeShown('garbage-a'), false)
  })
})
