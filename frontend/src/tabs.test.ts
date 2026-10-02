import test from 'node:test'
import assert from 'node:assert/strict'
import { tabFrom, tabHref } from './tabs.ts'

test('the address names the tab', () => {
  assert.equal(tabFrom('?tab=search'), 'search')
  assert.equal(tabFrom('?x=1&tab=settings'), 'settings')
})

test('no tab, or one that does not exist, is Profile', () => {
  assert.equal(tabFrom(''), 'profile')
  assert.equal(tabFrom('?tab=admin'), 'profile')
  assert.equal(tabFrom('?tab=constructor'), 'profile')
})

test("a tab's address keeps the rest of the query and drops the anchor", () => {
  assert.equal(
    tabHref('http://127.0.0.1:8700/?x=1&tab=profile#profile-skills', 'search'),
    'http://127.0.0.1:8700/?x=1&tab=search',
  )
  assert.equal(tabHref('http://127.0.0.1:8700/', 'settings'), 'http://127.0.0.1:8700/?tab=settings')
})
