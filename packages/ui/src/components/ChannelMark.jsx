import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * ChannelMark — 채널 문자 마크 (C · N · 11 · E · ALL).
 *
 * ★색만으로 채널을 구분하지 않는다. 4개 채널을 색으로만 나누려 하면 색약(적·녹)
 *   조건에서 분리도가 기준 아래로 떨어진다. 문자 마크가 1차 식별이고 색은 보조다.
 *
 * Props:
 *   channel: 'coupang' | 'smartstore' | 'elevenst' | 'esm' | 'all'
 *   mark:    표시 문자 (서버 /api/pa/accounts 가 내려준 값)
 *   size:    'sm' | 'md'
 *   muted:   true 면 색을 끄고 점선 테두리 — 진입 불가 채널용
 */
/* 채널색은 라이트·다크 양쪽에서 채도가 유지되므로 글자는 흰색 고정.
   'all' 만 중립 잉크라 다크에서 배경이 밝아진다 — 글자를 표면색으로 뒤집어야
   흰 배경에 흰 글자로 묻히지 않는다. */
const BG = {
  coupang:    'bg-channel-coupang text-white',
  smartstore: 'bg-channel-smartstore text-white',
  elevenst:   'bg-channel-elevenst text-white',
  esm:        'bg-channel-esm text-white',
  all:        'bg-ink-900 text-surface',
};

export function ChannelMark({ channel = 'all', mark, size = 'sm', muted = false, className }) {
  const label = mark || (channel === 'all' ? 'ALL' : channel.slice(0, 1).toUpperCase());
  return (
    <span
      aria-hidden="true"
      className={cx(
        'inline-grid flex-none place-items-center rounded font-mono font-bold tracking-tight',
        size === 'md' ? 'h-6 w-6 text-[11px]' : 'h-[18px] w-[18px] text-[9.5px]',
        muted
          ? 'border border-dashed border-line-strong text-ink-400'
          : (BG[channel] || BG.all),
        className,
      )}
    >
      {label}
    </span>
  );
}
