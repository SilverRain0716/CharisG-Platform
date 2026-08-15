import React from 'react';
import { cx } from '../utils/cx.js';

export function Input({ label, error, hint, className, ...rest }) {
  return (
    <label className="block">
      {label && <span className="mb-1 block text-[11px] font-medium text-ink-600">{label}</span>}
      <input
        className={cx(
          'h-8 w-full rounded border border-line bg-surface px-2.5 text-[13px] text-ink-900',
          'placeholder:text-ink-400',
          'focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent',
          error && 'border-signal-err focus:border-signal-err focus:ring-signal-err',
          className,
        )}
        {...rest}
      />
      {error && <span className="mt-1 block text-[11px] text-signal-err">{error}</span>}
      {!error && hint && <span className="mt-1 block text-[11px] text-ink-400">{hint}</span>}
    </label>
  );
}
