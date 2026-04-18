import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * Sidebar — 앱별 사이드 네비게이션.
 *
 * Props:
 *   items:    [{ id, label, icon, href, badge?, active? }]
 *   theme:    'ds' | 'pa' | 'shell'  (활성 강조 색)
 *   onSelect: (id) => void
 */
export function Sidebar({ items = [], theme = 'shell', onSelect, header }) {
  const accent = {
    ds:    'border-brand-ds-500 bg-brand-ds-50 text-brand-ds-700',
    pa:    'border-brand-pa-500 bg-brand-pa-50 text-brand-pa-700',
    shell: 'border-brand-shell bg-ink-100 text-ink-900',
  }[theme];

  return (
    <aside className="sticky top-14 flex h-[calc(100vh-3.5rem)] w-60 shrink-0 flex-col border-r border-ink-200 bg-white">
      {header && <div className="border-b border-ink-200">{header}</div>}
      <nav className="flex-1 overflow-y-auto p-3">
        <ul className="space-y-1">
          {items.map((it) => (
            <li key={it.id}>
              <a
                href={it.href}
                onClick={(e) => {
                  if (onSelect) {
                    e.preventDefault();
                    onSelect(it.id);
                  }
                }}
                className={cx(
                  'group flex items-center gap-3 rounded-md border-l-2 border-transparent px-3 py-2 text-sm font-medium',
                  it.active
                    ? accent
                    : 'text-ink-600 hover:bg-ink-100 hover:text-ink-900',
                )}
              >
                {it.icon && <span className="text-base opacity-80">{it.icon}</span>}
                <span className="flex-1">{it.label}</span>
                {it.badge !== undefined && it.badge !== null && (
                  <span className="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-ink-200 px-1.5 text-[11px] font-semibold text-ink-700">
                    {it.badge}
                  </span>
                )}
              </a>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}
