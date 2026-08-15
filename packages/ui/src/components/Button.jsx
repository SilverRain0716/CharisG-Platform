import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * Button.
 *
 * primary 는 고정색이 아니라 현재 채널 액센트(--accent)를 쓴다. 어느 채널에
 * 있는지가 주 버튼 색으로도 드러나야, 계정을 잘못 보고 등록을 거는 사고가 준다.
 *
 * variant: primary | secondary | ghost | danger | (구버전 호환: pa, ds)
 */
const VARIANTS = {
  primary:   'bg-accent text-accent-fg border-accent hover:brightness-110',
  secondary: 'bg-surface text-ink-700 border-line hover:bg-sunken hover:text-ink-900',
  ghost:     'bg-transparent text-ink-600 border-transparent hover:bg-sunken hover:text-ink-900',
  danger:    'bg-soft-err text-signal-err border-signal-err/30 hover:brightness-105',
};
// 예전 화면들이 variant="pa" / "ds" 를 쓰고 있다 — 채널 액센트로 흘려보낸다.
VARIANTS.pa = VARIANTS.primary;
VARIANTS.ds = VARIANTS.primary;

const SIZES = {
  sm: 'h-[26px] px-2.5 text-xs',
  md: 'h-8 px-3 text-[13px]',
  lg: 'h-9 px-4 text-[13px]',
};

export function Button({ variant = 'secondary', size = 'md', children, className, ...rest }) {
  return (
    <button
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded border font-medium transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        VARIANTS[variant] || VARIANTS.secondary,
        SIZES[size] || SIZES.md,
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
