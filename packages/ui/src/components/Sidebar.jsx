import React, { useState } from 'react';
import { cx } from '../utils/cx.js';

/**
 * Sidebar — 콘솔 좌측 네비게이션.
 *
 * Props:
 *   items: [{ id, label, icon, href, badge?, badgeTone?: 'crit'|'warn', active? }]
 *          { type: 'group', id, label, scope?: 'channel'|'common' } 는 구분 헤더.
 *
 * ★scope 뱃지가 중요하다. 채널이 전역 상태가 되면 "이 메뉴가 채널에 종속인지
 *   아닌지"가 흐려진다. 소싱·디스커버리·상품 마스터는 아마존 원천 데이터라
 *   채널과 무관한데, 채널 탭 아래 같이 놓이면 "채널을 바꾸면 소싱 목록도
 *   바뀌나?" 하는 혼란이 생긴다. 그래서 그룹 헤더에 범위를 명시한다.
 *
 * 모바일(< lg): 햄버거로 열고 닫는다. 데스크탑(lg+): 208px 고정.
 */
export function Sidebar({ items = [], onSelect, header, scopeLabel }) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="메뉴 열기"
        className="fixed left-2 top-2 z-50 inline-flex h-8 w-8 items-center justify-center rounded text-ink-700 hover:bg-ink-100 lg:hidden"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>

      {open && (
        <div className="fixed inset-0 z-40 bg-black/40 lg:hidden" onClick={() => setOpen(false)} aria-hidden="true" />
      )}

      {/* 모바일은 떠 있는 서랍, 데스크탑은 흐름 안의 sticky 기둥.
          fixed + spacer 조합을 쓰면 상단 헤더 높이가 채널/전체에 따라 달라질 때마다
          top 값을 손으로 맞춰야 해서, 헤더 바로 아래에 붙는 sticky 로 바꿨다. */}
      <aside
        className={cx(
          'fixed left-0 top-[84px] z-40 flex h-[calc(100vh-84px)] w-52 flex-col border-r border-line bg-sunken',
          'transition-transform duration-150 ease-out',
          open ? 'translate-x-0' : '-translate-x-full',
          'lg:sticky lg:top-[84px] lg:z-30 lg:h-[calc(100vh-84px)] lg:translate-x-0 lg:self-start',
        )}
      >
        <nav className="flex-1 overflow-y-auto p-2">
          <div className="mb-1 flex justify-end lg:hidden">
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="메뉴 닫기"
              className="inline-flex h-7 w-7 items-center justify-center rounded text-ink-500 hover:bg-ink-100"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>

          {header && <div className="mb-2 border-b border-line pb-2">{header}</div>}

          <ul className="space-y-0.5">
            {items.map((it) => {
              if (it.type === 'group') {
                return (
                  <li key={it.id} className="flex items-center gap-1.5 px-2 pb-1 pt-3">
                    <span className="text-2xs font-semibold uppercase tracking-[0.1em] text-ink-400">
                      {it.label}
                    </span>
                    {it.scope === 'channel' && (
                      <span className="rounded-full border border-accent/40 bg-accent/10 px-1.5 text-[9.5px] font-semibold text-accent">
                        {scopeLabel || '채널'}
                      </span>
                    )}
                    {it.scope === 'common' && (
                      <span className="rounded-full border border-line-strong px-1.5 text-[9.5px] font-semibold text-ink-400">
                        채널 무관
                      </span>
                    )}
                  </li>
                );
              }
              return (
                <li key={it.id}>
                  <a
                    href={it.href}
                    onClick={(e) => {
                      if (onSelect) {
                        e.preventDefault();
                        onSelect(it.id);
                        setOpen(false);
                      }
                    }}
                    className={cx(
                      'relative flex items-center gap-2 rounded px-2 py-1.5 text-[13px]',
                      it.active
                        ? 'bg-accent/10 font-semibold text-accent before:absolute before:-left-2 before:top-1.5 before:bottom-1.5 before:w-0.5 before:rounded-r before:bg-accent'
                        : 'text-ink-600 hover:bg-surface hover:text-ink-900',
                    )}
                  >
                    {it.icon && <span className="flex-none opacity-85">{it.icon}</span>}
                    <span className="flex-1 truncate">{it.label}</span>
                    {it.badge != null && it.badge !== 0 && (
                      <span
                        className={cx(
                          'rounded-full px-1.5 font-mono text-2xs tabular-nums',
                          it.badgeTone === 'crit' ? 'bg-soft-err text-signal-err'
                            : it.badgeTone === 'warn' ? 'bg-soft-warn text-signal-warn'
                              : 'bg-canvas text-ink-500',
                        )}
                      >
                        {typeof it.badge === 'number' ? it.badge.toLocaleString() : it.badge}
                      </span>
                    )}
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>
      </aside>
    </>
  );
}
