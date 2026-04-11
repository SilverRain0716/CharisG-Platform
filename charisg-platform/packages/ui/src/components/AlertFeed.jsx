import React from 'react';
import { cx } from '../utils/cx.js';
import { StatusBadge } from './StatusBadge.jsx';

/**
 * AlertFeed — 알림 피드.
 *
 * Props:
 *   items: [{ id, type, title, message, at }]   type: 'ok'|'warn'|'err'|'info'
 *   emptyText
 */
export function AlertFeed({ items = [], emptyText = '알림 없음' }) {
  if (items.length === 0) {
    return <div className="rounded-lg bg-white p-6 text-center text-sm text-ink-400 ring-1 ring-ink-100">{emptyText}</div>;
  }
  return (
    <ul className="divide-y divide-ink-100 rounded-lg bg-white ring-1 ring-ink-100">
      {items.map((it) => (
        <li key={it.id} className="flex items-start gap-3 px-4 py-3">
          <StatusBadge variant={it.type || 'info'}>{it.type || 'info'}</StatusBadge>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-ink-900">{it.title}</div>
            {it.message && <div className="mt-0.5 text-xs text-ink-500">{it.message}</div>}
          </div>
          {it.at && <div className="shrink-0 text-[11px] text-ink-400">{it.at}</div>}
        </li>
      ))}
    </ul>
  );
}
