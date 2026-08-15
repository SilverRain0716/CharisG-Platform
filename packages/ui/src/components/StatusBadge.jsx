import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * StatusBadge — 상태 표시 뱃지.
 *
 * 상태색(정상·주의·위험·진행)은 채널 식별색과 절대 겹치지 않는다. 채널색은
 * 크롬(탭·사이드바·주 버튼)에만, 상태색은 데이터 뱃지에만 쓴다.
 * 점 + 텍스트를 함께 두어 색만으로 상태를 읽지 않아도 되게 한다.
 *
 * variant: 'ok' | 'warn' | 'err' | 'crit' | 'info' | 'mute' | 'neutral'
 */
const STYLES = {
  ok:      'bg-soft-ok   text-signal-ok   ring-signal-ok/25',
  warn:    'bg-soft-warn text-signal-warn ring-signal-warn/30',
  err:     'bg-soft-err  text-signal-err  ring-signal-err/30',
  info:    'bg-soft-info text-signal-info ring-signal-info/25',
  mute:    'bg-sunken    text-ink-500     ring-line',
};
STYLES.crit = STYLES.err;
STYLES.neutral = STYLES.mute;

export function StatusBadge({ variant = 'neutral', dot = true, children, className }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset',
        STYLES[variant] || STYLES.neutral,
        className,
      )}
    >
      {dot && <i aria-hidden="true" className="h-[5px] w-[5px] rounded-full bg-current" />}
      {children}
    </span>
  );
}
