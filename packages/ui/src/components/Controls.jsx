import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * 목록 화면 공통 컨트롤 — Toolbar / FilterChip / LockChip / Segmented / EmptyState.
 */

/** 검색·필터가 놓이는 한 줄. 패널 헤더 바로 아래에 붙는다. */
export function Toolbar({ children, className }) {
  return (
    <div className={cx('flex flex-wrap items-center gap-2 border-b border-line px-3 py-2', className)}>
      {children}
    </div>
  );
}

/** 켜고 끄는 필터. 건수를 같이 보여줘야 누르기 전에 결과 크기를 안다. */
export function FilterChip({ active, count, children, className, ...rest }) {
  return (
    <button
      type="button"
      aria-pressed={!!active}
      className={cx(
        'inline-flex h-[26px] items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors',
        active
          ? 'border-accent/40 bg-accent/10 font-semibold text-accent'
          : 'border-line bg-surface text-ink-600 hover:bg-sunken hover:text-ink-900',
        className,
      )}
      {...rest}
    >
      {children}
      {count != null && (
        <span className="font-mono text-[11px] opacity-75 tabular-nums">
          {typeof count === 'number' ? count.toLocaleString() : count}
        </span>
      )}
    </button>
  );
}

/**
 * LockChip — 바꿀 수 없는 필터.
 *
 * 목록 화면에서 채널·계정을 고르는 필터를 없애고 이걸로 대체한다. 상단 탭과
 * 화면 안 필터가 둘 다 채널을 바꿀 수 있으면 서로 어긋난 상태가 생긴다.
 * 바꾸는 곳은 상단 한 곳뿐이라는 사실을 자물쇠로 알린다.
 */
export function LockChip({ children, title = '상단에서 변경' }) {
  return (
    <span
      title={title}
      className="inline-flex h-[26px] cursor-not-allowed items-center gap-1.5 rounded-full border border-dashed border-line-strong px-2.5 text-xs text-ink-400"
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="4" y="11" width="16" height="10" rx="2" />
        <path d="M8 11V7a4 4 0 0 1 8 0v4" />
      </svg>
      {children}
    </span>
  );
}

/** 배타 선택 (기간, 표시 모드 등). */
export function Segmented({ options = [], value, onChange, size = 'md', className }) {
  return (
    <div className={cx('inline-flex overflow-hidden rounded border border-line bg-surface', className)} role="group">
      {options.map((o, i) => {
        const v = typeof o === 'string' ? o : o.value;
        const label = typeof o === 'string' ? o : o.label;
        const on = v === value;
        return (
          <button
            key={v}
            type="button"
            aria-pressed={on}
            onClick={() => onChange?.(v)}
            className={cx(
              i > 0 && 'border-l border-line',
              size === 'sm' ? 'px-2 py-0.5 text-[11.5px]' : 'px-2.5 py-1 text-xs',
              on ? 'bg-sunken font-semibold text-ink-900' : 'text-ink-500 hover:text-ink-900',
            )}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

/** 데이터가 없을 때. 왜 비었는지와 다음에 뭘 할 수 있는지를 같이 말한다. */
export function EmptyState({ title, description, action, icon, className }) {
  return (
    <div className={cx('grid justify-items-center gap-2 px-4 py-10 text-center', className)}>
      {icon}
      <div className="text-sm font-semibold text-ink-900">{title}</div>
      {description && <p className="max-w-[46ch] text-[12.5px] text-ink-500">{description}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

/** 로딩 자리표시. 높이를 미리 잡아 두면 데이터가 와도 레이아웃이 튀지 않는다. */
export function Skeleton({ h = 16, w = '100%', className }) {
  return (
    <span
      aria-hidden="true"
      className={cx('block animate-pulse rounded bg-ink-200', className)}
      style={{ height: h, width: w }}
    />
  );
}
