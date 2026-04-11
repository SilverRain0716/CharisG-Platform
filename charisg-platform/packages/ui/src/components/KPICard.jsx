import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * KPICard — 핵심 지표 1개 표시 카드.
 *
 * Props:
 *   label:  string
 *   value:  string | number
 *   delta:  number (% 변화량, +/-)
 *   trend:  'up' | 'down' | 'flat'
 *   accent: 'ds' | 'pa' | 'shell'
 *   hint:   string
 */
export function KPICard({ label, value, delta, trend, accent = 'shell', hint }) {
  const ring = {
    ds:    'ring-brand-ds-100',
    pa:    'ring-brand-pa-100',
    shell: 'ring-ink-100',
  }[accent];

  const trendColor = {
    up:   'text-signal-ok',
    down: 'text-signal-err',
    flat: 'text-ink-400',
  }[trend || 'flat'];

  return (
    <div className={cx('rounded-lg bg-white p-5 shadow-card ring-1', ring)}>
      <div className="text-xs font-medium uppercase tracking-wide text-ink-500">
        {label}
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <div className="text-2xl font-semibold tracking-tight text-ink-900">
          {value}
        </div>
        {delta != null && (
          <div className={cx('text-xs font-semibold', trendColor)}>
            {trend === 'up' && '▲'}
            {trend === 'down' && '▼'}
            {Math.abs(delta).toFixed(1)}%
          </div>
        )}
      </div>
      {hint && <div className="mt-1 text-xs text-ink-400">{hint}</div>}
    </div>
  );
}
