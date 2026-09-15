import { badgeFor } from './styles'
import type { Badge } from './styles'

export const ENGINE_STATUS_LABELS: Record<string, Badge> = {
  ok: { label: 'Claude ready', tone: 'ok' },
  unprobed: { label: 'Claude not yet probed', tone: 'neutral' },
  waiting_usage_exhausted: {
    label: 'Waiting — usage limit reached',
    tone: 'warn',
  },
  auth_required: {
    label: 'Sign in to Claude required',
    tone: 'danger',
  },
  config_error: { label: 'Engine configuration error', tone: 'danger' },
}

export function engineBadge(status: string): Badge {
  return badgeFor(ENGINE_STATUS_LABELS, status)
}

export function retryText(seconds: number | null): string | null {
  if (seconds == null || seconds <= 0) return null
  const mins = Math.ceil(seconds / 60)
  return mins >= 60
    ? `~${Math.floor(mins / 60)}h ${mins % 60}m`
    : `~${mins}m`
}
