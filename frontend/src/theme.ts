import { useCallback, useEffect, useState } from 'react'

/** `system` means no override: the page follows the operating system
 * through `color-scheme: light dark` in index.css. */
export type ThemePreference = 'system' | 'light' | 'dark'

const KEY = 'theme'

function isOverride(value: unknown): value is 'light' | 'dark' {
  return value === 'light' || value === 'dark'
}

/** What this browser has stored. Missing, unreadable or unrecognised all
 * read as System — the only safe meaning of a value nobody chose. */
export function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(KEY)
    return isOverride(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

/** Put a preference on the document. The bootstrap script in index.html
 * does the same two things before the stylesheet exists; keep them in
 * step. The attribute is what index.css switches on. The inline
 * color-scheme is what paints the empty shell before that stylesheet has
 * arrived, and it is removed again for System so the media default rules. */
export function applyPreference(preference: ThemePreference): void {
  const root = document.documentElement
  if (preference === 'system') {
    delete root.dataset.theme
    root.style.removeProperty('color-scheme')
  } else {
    root.dataset.theme = preference
    root.style.colorScheme = preference
  }
}

function storePreference(preference: ThemePreference): void {
  try {
    if (preference === 'system') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, preference)
  } catch {
    // Blocked or full. The document is already switched; the choice
    // simply does not outlive this tab.
  }
}

/** The preference as React state, kept in step with other tabs.
 *
 * Mounted once, in App, for the life of the document. It must not belong
 * to a page: the listener below has to be up whichever tab is showing,
 * and when storage is blocked this state is the only record of the
 * choice, so unmounting with a page would reset it to System.
 *
 * Setting it switches THIS document at once and writes storage. The
 * `storage` event never fires in the document that wrote — only in the
 * others — so the listener exists purely for changes made elsewhere:
 * another tab choosing, or site data being cleared (`key === null`). It
 * re-reads storage rather than trusting the event, so removal and clear
 * need no special case.
 */
export function usePreference(): [ThemePreference, (next: ThemePreference) => void] {
  const [preference, setState] = useState<ThemePreference>(readPreference)

  const setPreference = useCallback((next: ThemePreference) => {
    applyPreference(next)
    storePreference(next)
    setState(next)
  }, [])

  useEffect(() => {
    // The bootstrap normally did this already; doing it again is free and
    // makes the control right even if that script did not run.
    applyPreference(readPreference())
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== KEY) return
      const next = readPreference()
      applyPreference(next)
      setState(next)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  return [preference, setPreference]
}
