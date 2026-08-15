import React from 'react';
import { cx } from '../utils/cx.js';
import { ChannelMark } from './ChannelMark.jsx';

/**
 * ChannelTabs — 상단 1행. 채널을 고르면 콘솔 전체가 그 채널로 잠긴다.
 *
 * 진입 불가 채널(ESM 처럼 공개 API 가 없는 곳)은 눌러도 빈 대시보드가 열리지
 * 않게 잠근다. "계정만 없는 것"과 "연동할 방법이 없는 것"은 다른 상태라,
 * 전자는 계정 줄의 빈 슬롯으로, 후자는 여기서 잠근 탭으로 드러낸다.
 *
 * Props:
 *   channels: [{ channel, label, mark, usable, badge? }]
 *   value:    현재 채널
 *   onChange: (channel) => void
 *   onBlocked:(channel) => void   진입 불가 탭을 눌렀을 때
 */
export function ChannelTabs({ channels = [], value = 'all', onChange, onBlocked }) {
  const items = [{ channel: 'all', label: '전체', mark: 'ALL', usable: true }, ...channels];

  return (
    <nav className="flex min-w-0 items-stretch gap-0.5 overflow-x-auto" aria-label="채널 선택">
      {items.map((c) => {
        const active = c.channel === value;
        const blocked = !c.usable;
        return (
          <button
            key={c.channel}
            type="button"
            aria-current={active ? 'page' : undefined}
            aria-disabled={blocked || undefined}
            onClick={() => (blocked ? onBlocked?.(c.channel) : onChange?.(c.channel))}
            className={cx(
              'flex h-11 items-center gap-2 whitespace-nowrap border-b-2 px-3 text-[13px] transition-colors',
              active
                ? 'border-accent bg-accent/10 font-semibold text-ink-900'
                : 'border-transparent text-ink-500 hover:bg-sunken hover:text-ink-900',
              blocked && 'cursor-not-allowed text-ink-400 hover:bg-transparent hover:text-ink-400',
            )}
          >
            <ChannelMark channel={c.channel} mark={c.mark} muted={blocked} />
            <span>{c.label}</span>
            {blocked ? (
              <span className="rounded-full border border-dashed border-line-strong px-1.5 text-2xs text-ink-400">
                API 없음
              </span>
            ) : c.badge ? (
              <span className="rounded-full bg-soft-err px-1.5 font-mono text-2xs font-semibold text-signal-err">
                {c.badge}
              </span>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}
