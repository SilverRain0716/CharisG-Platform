import React from 'react';
import { cx } from '../utils/cx.js';

export function Card({ title, action, children, className, padded = true }) {
  return (
    <section className={cx('rounded-lg bg-white shadow-card ring-1 ring-ink-100', className)}>
      {(title || action) && (
        <header className="flex items-center justify-between border-b border-ink-100 px-5 py-3">
          {title && <h3 className="text-sm font-semibold text-ink-900">{title}</h3>}
          {action}
        </header>
      )}
      <div className={padded ? 'p-5' : ''}>{children}</div>
    </section>
  );
}
