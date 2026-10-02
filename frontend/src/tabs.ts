export const TABS = [
  { id: 'profile', label: 'Profile' },
  { id: 'search', label: 'Search' },
  { id: 'settings', label: 'Settings' },
] as const

export type TabId = (typeof TABS)[number]['id']

/** The tab an address names in `?tab=`: Profile when it names none, or
 * one that does not exist. */
export function tabFrom(search: string): TabId {
  const named = new URLSearchParams(search).get('tab')
  return TABS.find((t) => t.id === named)?.id ?? 'profile'
}

/** The address of a tab: the page's own, with `?tab=` set to it and any
 * other query kept. A section anchor belongs to the tab it was on, so it
 * is not carried to another. */
export function tabHref(href: string, id: TabId): string {
  const url = new URL(href)
  url.searchParams.set('tab', id)
  url.hash = ''
  return url.toString()
}
