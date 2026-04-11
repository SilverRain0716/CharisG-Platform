import React from 'react';
import { cx } from '../utils/cx.js';

export function Button({
  variant = 'primary',
  size = 'md',
  children,
  className,
  ...rest
}) {
  const variants = {
    primary:   'bg-brand-shell text-white hover:bg-ink-800',
    ds:        'bg-brand-ds-600 text-white hover:bg-brand-ds-700',
    pa:        'bg-brand-pa-600 text-white hover:bg-brand-pa-700',
    secondary: 'bg-white text-ink-900 ring-1 ring-ink-200 hover:bg-ink-50',
    ghost:     'bg-transparent text-ink-700 hover:bg-ink-100',
    danger:    'bg-signal-err text-white hover:opacity-90',
  };
  const sizes = {
    sm: 'h-8 px-3 text-xs',
    md: 'h-9 px-4 text-sm',
    lg: 'h-10 px-5 text-sm',
  };
  return (
    <button
      className={cx(
        'inline-flex items-center justify-center rounded-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
        variants[variant],
        sizes[size],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
