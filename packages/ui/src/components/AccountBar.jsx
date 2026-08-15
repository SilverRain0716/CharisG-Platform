import React from 'react';
import { cx } from '../utils/cx.js';
import { ChannelMark } from './ChannelMark.jsx';
import { StatusBadge } from './StatusBadge.jsx';

/**
 * AccountBar — 상단 2행. 선택된 채널의 계정(사업자)을 고른다.
 *
 * 왜 뱃지가 아니라 세그먼트인가: 뱃지는 읽어야 알지만 눌린 세그먼트는 형태
 * 자체가 상태다. 어느 계정에 등록·중지를 걸고 있는지 놓치면 사고가 나므로,
 * 이 줄은 스크롤해도 사라지지 않는다.
 *
 * 연결 안 된 계정 슬롯은 지우지 않고 점선으로 남긴다 — "이 채널에 계정이
 * 하나 더 있는데 아직 안 붙었다"는 사실 자체가 정보다.
 *
 * Props:
 *   channel:   현재 채널 코드
 *   label:     채널 이름
 *   mark:      채널 문자 마크
 *   accounts:  [{ account_key, label, store_name, vendor_id, status, usable, note }]
 *   value:     현재 account_key
 *   onChange:  (account_key) => void
 *   right:     오른쪽에 붙일 노드 (요약 수치 등)
 */

// seller_accounts.status → 사람이 읽는 말 + 색. DB 값이 단일 출처라 여기서
// 새 어휘를 만들지 않는다.
const STATUS = {
  active:   { tone: 'ok',   text: '영업 중' },
  ready:    { tone: 'info', text: '준비됨 · 리스팅 0' },
  reducing: { tone: 'warn', text: '정리 중' },
  pending:  { tone: 'mute', text: '자격 신청중' },
  wiped:    { tone: 'mute', text: '전량 삭제됨' },
  unknown:  { tone: 'mute', text: '연동 수단 없음' },
};

export function AccountBar({ channel, label, mark, accounts = [], value, onChange, right }) {
  const current = accounts.find((a) => a.account_key === value);
  const meta = STATUS[current?.status] || null;

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-line bg-accent/[0.07] px-3 py-2">
      <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink-900">
        <ChannelMark channel={channel} mark={mark} size="md" />
        {label}
      </span>

      {accounts.length > 0 && (
        <div className="inline-flex overflow-hidden rounded border border-accent/40 bg-surface" role="group" aria-label="계정 선택">
          {accounts.map((a, i) => {
            const on = a.account_key === value;
            return (
              <button
                key={a.account_key}
                type="button"
                aria-pressed={on}
                onClick={() => onChange?.(a.account_key)}
                title={a.note || undefined}
                className={cx(
                  'flex items-center gap-2 px-3 py-1 text-xs transition-colors',
                  i > 0 && 'border-l border-accent/40',
                  on ? 'bg-accent font-semibold text-accent-fg' : 'text-ink-500 hover:bg-sunken hover:text-ink-900',
                  !a.usable && !on && 'border-dashed text-ink-400',
                )}
              >
                <span>
                  {a.label}
                  <span className="ml-1 opacity-70">({a.account_key === 'new' ? '신' : '구'})</span>
                </span>
                {a.store_name && <span className="font-mono text-2xs opacity-75">{a.store_name}</span>}
                {!a.usable && <span className="text-2xs opacity-75">· 미연결</span>}
              </button>
            );
          })}
        </div>
      )}

      <div className="ml-auto flex flex-wrap items-center gap-2 text-[11.5px] text-ink-500">
        {meta && <StatusBadge variant={meta.tone}>{meta.text}</StatusBadge>}
        {current?.vendor_id && <span className="font-mono text-2xs text-ink-400">{current.vendor_id}</span>}
        {right}
      </div>
    </div>
  );
}
