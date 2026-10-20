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

/** Opens each fold that hides `el`, so it can take the focus. A fold's
 * own summary shows while the fold is closed, and leaves it closed. */
export function reveal(el: HTMLElement) {
  for (let fold = el.closest('details'); fold; fold = fold.parentElement?.closest('details') ?? null) {
    if (!fold.open && !(el.matches('summary') && el.parentElement === fold)) fold.open = true
  }
}

/** The element holding the focus while `holds` claims it, for a caller
 * that puts the focus back when the page takes that element away. Only
 * the user lets it go: by focusing any other element, or by pressing the
 * pointer anywhere outside it (a press on nothing sends the focus to the
 * body). A blur the user did not cause keeps it held: the element
 * removed, or moved and given the focus back — the browser blurs a row a
 * keyed list re-inserts, and React refocuses it — and so does another
 * window taking the focus for a while. `holds` must be stable. */
export function useHeldFocus(holds: (el: HTMLElement) => boolean): RefObject<HTMLElement | null> {
  const held = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      const el = e.target
      held.current = el instanceof HTMLElement && holds(el) ? el : null
    }
    const onPointerDown = (e: PointerEvent) => {
      const el = e.target
      if (!(el instanceof Node && held.current?.contains(el))) held.current = null
    }
    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [holds])
  return held
}
