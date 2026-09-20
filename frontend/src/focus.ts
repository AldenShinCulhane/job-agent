import { useCallback, useEffect, useState } from 'react'
import type { RefObject } from 'react'

/** Where keyboard focus lands after an action removes the control that
 * had it — a bar that disappears, a button replaced by a chip, fields
 * that fold. The browser would drop focus to the body and a keyboard
 * user would be back at the top of the page.
 *
 * `land(ref)` asks for focus on `ref` once the render the action caused
 * has committed. It is an effect, not a timer, because React commits an
 * update made after an `await` on its own schedule, and a timer can run
 * before the element the ref points at exists. */
export function useFocusLanding(): (ref: RefObject<HTMLElement | null>) => void {
  const [target, setTarget] = useState<RefObject<HTMLElement | null> | null>(null)
  useEffect(() => {
    if (!target) return
    target.current?.focus()
    setTarget(null)
  }, [target])
  return useCallback((ref: RefObject<HTMLElement | null>) => setTarget(ref), [])
}
