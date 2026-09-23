import test from 'node:test'
import assert from 'node:assert/strict'
import {
  agoText,
  clockText,
  engineBadge,
  modelLabel,
  otherProvider,
  retryText,
  selectedEngine,
  selectionText,
  validModelId,
} from './engineStatus.ts'
import type { EnginesView, ProviderView } from './api.ts'

function provider(over: Partial<ProviderView>): ProviderView {
  return {
    provider: 'claude', status: 'ok', model: null, retry_in_seconds: null,
    next_retry_at: null, limit_resets_at: null, episode: null, last_error: null,
    credentials: null, verified_at: null, catalog: null, catalog_error: null,
    models: {}, ...over,
  }
}

function view(over: Partial<EnginesView> = {}): EnginesView {
  return {
    selection: { provider: 'codex', model: 'gpt-x' },
    providers: {
      claude: provider({ auto_model: null }),
      codex: provider({
        provider: 'codex', model: 'gpt-x',
        catalog: [{ id: 'gpt-x', label: 'GPT X', description: '', efforts: [] }],
      }),
    },
    ...over,
  }
}

test('the badge names the engine in use', () => {
  assert.equal(engineBadge({ provider: 'codex', status: 'ok' }).label, 'Codex ready')
  assert.equal(engineBadge({ provider: 'claude', status: 'auth_required' }).label,
    'Sign in to Claude required')
  assert.equal(engineBadge({ provider: 'claude', status: 'not_entitled' }).tone, 'danger')
  assert.equal(engineBadge({ provider: 'codex', status: 'waiting_usage_exhausted' }).tone, 'warn')
  // An unknown status still renders, under its own name.
  assert.deepEqual(engineBadge({ provider: 'codex', status: 'rebooting' }),
    { label: 'rebooting', tone: 'neutral' })
})

test('the selected engine and its model names come from the one view', () => {
  const v = view()
  assert.equal(selectedEngine(v).provider, 'codex')
  assert.equal(modelLabel(v, 'codex', 'gpt-x'), 'GPT X')
  assert.equal(modelLabel(v, 'codex', 'unlisted'), 'unlisted')
  assert.equal(modelLabel(v, 'claude', null), null)
  assert.equal(selectionText(v), 'GPT X')
  assert.equal(otherProvider('codex'), 'claude')
})

test("Claude's default says what it settled on once it has", () => {
  const unsettled = view({ selection: { provider: 'claude', model: null } })
  assert.match(selectionText(unsettled), /Fable 5 if your plan has it/)
  const settled = view({ selection: { provider: 'claude', model: null } })
  settled.providers.claude.auto_model = 'claude-opus-5'
  assert.equal(selectionText(settled), 'the default (claude-opus-5)')
})

test('model ids are held to the same shape the server accepts', () => {
  for (const ok of ['gpt-5.5', 'claude-opus-5', 'opus[1m]', 'org/model:v1']) {
    assert.ok(validModelId(ok), ok)
  }
  for (const bad of ['', '-flag', 'a b', 'x; rm -rf /', 'a'.repeat(129)]) {
    assert.ok(!validModelId(bad), bad)
  }
})

test('times read as clock times, with the day only when it is not today', () => {
  const now = new Date(2026, 8, 23, 9, 0)
  const later = new Date(2026, 8, 23, 15, 0).toISOString()
  const tomorrow = new Date(2026, 8, 24, 15, 0).toISOString()
  assert.ok(!/Sep|Thu/.test(clockText(later, now) ?? ''), clockText(later, now) ?? '')
  assert.match(clockText(tomorrow, now) ?? '', /Sep|24/)
  assert.equal(clockText(null, now), null)
  assert.equal(clockText('not a time', now), null)
})

test('ago and retry texts are coarse', () => {
  const now = Date.parse('2026-09-23T12:00:00Z')
  assert.equal(agoText('2026-09-23T11:59:40Z', now), 'just now')
  assert.equal(agoText('2026-09-23T11:48:00Z', now), '12 min ago')
  assert.equal(agoText('2026-09-23T09:00:00Z', now), '3 h ago')
  assert.equal(agoText(null, now), null)
  assert.equal(retryText(600), '~10m')
  assert.equal(retryText(3 * 3600 + 60), '~3h 1m')
  assert.equal(retryText(0), null)
})
