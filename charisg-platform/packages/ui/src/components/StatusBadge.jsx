import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * StatusBadge — 상태 표시 뱃지.
 *
 * Props:
 *   variant: 'ok' | 'warn' | 'err' | 'info' | 'neutral'
 *   children
 */
export function StatusBadge({ variant = 'neutral', children, className }) {
  const styles = {
    ok:      'bg-emerald-50 text-emerald-700 ring-emerald-100',
    warn:    'bg-amber-50  text-amber-700  ring-amber-100',
    err:     'bg-red-50    text-red-700    ring-red-100',
    info:    'bg-blue-50   text-blue-700   ring-blue-100',
    neutral: 'bg-ink-50    text-ink-700    ring-ink-200',
  }[variant];

  return (
    <span
      className={cx(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset',
        styles,
        className,
      )}
    >
      {children}
    </span>
  );
}
