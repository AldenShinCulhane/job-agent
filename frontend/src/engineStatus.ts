export const ENGINE_STATUS_LABELS: Record<string, { label: string; tone: string }> = {
  ok: { label: 'Claude ready', tone: 'bg-emerald-100 text-emerald-800' },
  unprobed: { label: 'Claude not yet probed', tone: 'bg-slate-100 text-slate-600' },
  waiting_usage_exhausted: {
    label: 'Waiting — usage limit reached',
    tone: 'bg-amber-100 text-amber-800',
  },
  auth_required: {
    label: 'Sign in to Claude required',
    tone: 'bg-rose-100 text-rose-800',
  },
  config_error: { label: 'Engine configuration error', tone: 'bg-rose-100 text-rose-800' },
}

export function engineBadge(status: string): { label: string; tone: string } {
  return ENGINE_STATUS_LABELS[status] ?? { label: status, tone: 'bg-slate-100 text-slate-600' }
}

export function retryText(seconds: number | null): string | null {
  if (seconds == null || seconds <= 0) return null
  const mins = Math.ceil(seconds / 60)
  return mins >= 60
    ? `~${Math.floor(mins / 60)}h ${mins % 60}m`
    : `~${mins}m`
}
