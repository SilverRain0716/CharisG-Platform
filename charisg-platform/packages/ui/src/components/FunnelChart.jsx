import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * FunnelChart — 파이프라인 단계별 건수 + 전환율 표시.
 *
 * Props:
 *   stages: [{ key, label, count, color? }]
 *   onStageClick: (key) => void
 */
export function FunnelChart({ stages = [], onStageClick }) {
  const max = Math.max(1, ...stages.map((s) => s.count || 0));

  return (
    <div className="space-y-2">
      {stages.map((s, idx) => {
        const pct = ((s.count || 0) / max) * 100;
        const conversion =
          idx > 0 && stages[idx - 1].count
            ? (((s.count || 0) / stages[idx - 1].count) * 100).toFixed(1)
            : null;

        return (
          <button
            key={s.key}
            type="button"
            disabled={!onStageClick}
            onClick={() => onStageClick && onStageClick(s.key)}
            className={cx(
              'group flex w-full items-center gap-3 text-left',
              onStageClick && 'cursor-pointer',
            )}
          >
            <div className="w-32 shrink-0 text-sm font-medium text-ink-700">
              {s.label}
            </div>
            <div className="relative h-9 flex-1 overflow-hidden rounded-md bg-ink-100">
              <div
                className={cx(
                  'absolute inset-y-0 left-0 transition-all',
                  s.color || 'bg-brand-ds-500',
                  onStageClick && 'group-hover:opacity-90',
                )}
                style={{ width: `${pct}%` }}
              />
              <div className="absolute inset-0 flex items-center justify-between px-3 text-xs font-semibold text-ink-900">
                <span>{(s.count || 0).toLocaleString()}</span>
                {conversion && <span className="text-ink-500">{conversion}%</span>}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
