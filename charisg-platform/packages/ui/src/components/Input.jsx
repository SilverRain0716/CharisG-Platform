import React from 'react';
import { cx } from '../utils/cx.js';

export function Input({ label, error, hint, className, ...rest }) {
  return (
    <label className="block">
      {label && <span className="mb-1 block text-xs font-medium text-ink-700">{label}</span>}
      <input
        className={cx(
          'h-9 w-full rounded-md border border-ink-200 bg-white px-3 text-sm text-ink-900 placeholder:text-ink-400',
          'focus:border-brand-shell focus:outline-none focus:ring-1 focus:ring-brand-shell',
          error && 'border-signal-err focus:border-signal-err focus:ring-signal-err',
          className,
        )}
        {...rest}
      />
      {error && <span className="mt-1 block text-xs text-signal-err">{error}</span>}
      {!error && hint && <span className="mt-1 block text-xs text-ink-400">{hint}</span>}
    </label>
  );
}
