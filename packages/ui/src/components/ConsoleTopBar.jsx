import React from 'react';
import { ThemeToggle } from '../theme.jsx';

/**
 * ConsoleTopBar — 상단 1행의 좌우 껍데기. 가운데 채널 탭은 children 으로 받는다.
 *
 * 예전 GlobalTopBar 는 Hub/DS/PA 앱을 오가는 탭이었다. 드랍쉬핑을 접고 Hub 를
 * 없앴으므로 그 자리는 이제 채널 스위처가 쓴다.
 */
export function ConsoleTopBar({ children, user, onLogout, onLogoClick, right }) {
  return (
    <header className="flex h-11 items-center gap-3 border-b border-line bg-surface px-3">
      <button
        type="button"
        onClick={onLogoClick}
        className="flex flex-none items-center gap-2 text-[13.5px] font-semibold tracking-tight text-ink-900"
      >
        <span className="h-[17px] w-[17px] rounded bg-gradient-to-br from-channel-smartstore to-channel-coupang" />
        <span className="hidden sm:inline">Charis G</span>
      </button>

      {children}

      <div className="ml-auto flex flex-none items-center gap-2">
        {right}
        <ThemeToggle />
        <div className="flex items-center gap-2">
          <div className="grid h-6 w-6 place-items-center rounded-full border border-line bg-sunken text-[11px] font-bold text-ink-600">
            {user?.name?.[0] || 'U'}
          </div>
          {onLogout && (
            <button
              onClick={onLogout}
              className="rounded px-1.5 py-1 text-[11px] text-ink-500 hover:bg-sunken hover:text-ink-900"
            >
              로그아웃
            </button>
          )}
        </div>
      </div>
    </header>
  );
}
