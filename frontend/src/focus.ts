import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

/** Where keyboard focus lands after an action removes the control that
 * had it — a bar that disappears, a button replaced by a chip, fields
 * that fold. The browser would drop focus to the body and a keyboard
 * user would be back at the top of the page.
 *
 * `land(ref)` asks for focus on `ref` at the first commit after which the
 * element exists. It is an effect, not a timer, because React commits an
 * update made after an `await` on its own schedule, and a timer can run
 * before the element the ref points at exists. The request is kept, not
 * consumed, while its target is unmounted: the control an action produces
 * can arrive only with a later response (the first search's "Edit search"
 * button exists once the accepted run has been read back). A newer
 * request replaces an unmet one. */
export function useFocusLanding(): (ref: RefObject<HTMLElement | null>) => void {
  const pending = useRef<RefObject<HTMLElement | null> | null>(null)
  const [, rerender] = useState(0)
  useEffect(() => {
    const el = pending.current?.current
    if (!el) return
    pending.current = null
    el.focus()
  })
  return useCallback((ref: RefObject<HTMLElement | null>) => {
    pending.current = ref
    rerender((n) => n + 1)
  }, [])
}
