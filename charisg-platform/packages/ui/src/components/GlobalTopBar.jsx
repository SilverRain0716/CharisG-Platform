import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * GlobalTopBar — 3개 앱(Hub/DS/PA) 공통 상단 바.
 *
 * 구성:
 *   왼쪽   : Charis G 로고 (→Hub 복귀)
 *   중앙   : 앱 전환 탭 (Hub / Dropshipping / Purchase Agent)
 *           활성 앱 underline. 비활성 탭에 미처리 건수 배지
 *   오른쪽 : 알림 아이콘 + 프로필
 *
 * Props:
 *   activeApp:    'hub' | 'dropshipping' | 'purchase'
 *   summary:      { ds: { pendingCount, kpis: [{label, value}] }, pa: { ... } }
 *   user:         { name, email }
 *   onAppChange:  (appName) => void
 *   onLogoClick:  () => void
 *   onLogout:     () => void
 *   notifications: number
 */
export function GlobalTopBar({
  activeApp = 'hub',
  summary = {},
  user,
  onAppChange,
  onLogoClick,
  onLogout,
  notifications = 0,
}) {
  const tabs = [
    { id: 'hub',          label: 'Hub',           href: '/' },
    { id: 'dropshipping', label: 'Dropshipping',  href: '/dropshipping/', badge: summary.ds?.pendingCount },
    { id: 'purchase',     label: 'Purchase Agent', href: '/purchase/',     badge: summary.pa?.pendingCount },
  ];

  return (
    <header className="sticky top-0 z-40 w-full border-b border-ink-200 bg-white/80 backdrop-blur supports-[backdrop-filter]:bg-white/60">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center px-6">
        {/* Logo */}
        <button
          onClick={onLogoClick}
          className="flex items-center gap-2 text-ink-900 hover:text-brand-shell"
        >
          <span className="inline-block h-7 w-7 rounded-md bg-gradient-to-br from-brand-ds-500 to-brand-pa-500" />
          <span className="text-base font-semibold tracking-tight">Charis G</span>
        </button>

        {/* Tabs */}
        <nav className="ml-10 flex items-center gap-1">
          {tabs.map((t) => {
            const active = t.id === activeApp;
            return (
              <a
                key={t.id}
                href={t.href}
                onClick={(e) => {
                  if (onAppChange) {
                    e.preventDefault();
                    onAppChange(t.id);
                    window.location.href = t.href;
                  }
                }}
                className={cx(
                  'group relative inline-flex h-14 items-center gap-2 px-4 text-sm font-medium transition-colors',
                  active ? 'text-ink-900' : 'text-ink-500 hover:text-ink-900',
                )}
              >
                <span>{t.label}</span>
                {t.badge ? (
                  <span className="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-signal-warn px-1.5 text-[11px] font-semibold text-white">
                    {t.badge}
                  </span>
                ) : null}
                {active && <span className="absolute inset-x-3 bottom-0 h-0.5 bg-brand-shell" />}
              </a>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <button
            type="button"
            className="relative rounded-md p-2 text-ink-500 hover:bg-ink-100 hover:text-ink-900"
            aria-label="알림"
          >
            <BellIcon />
            {notifications > 0 && (
              <span className="absolute right-1 top-1 inline-block h-2 w-2 rounded-full bg-signal-err" />
            )}
          </button>

          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-ink-200 text-xs font-semibold text-ink-700">
              {user?.name?.[0] || 'U'}
            </div>
            <div className="hidden text-right text-xs leading-tight md:block">
              <div className="font-medium text-ink-900">{user?.name || '사용자'}</div>
              <div className="text-ink-500">{user?.email || ''}</div>
            </div>
            {onLogout && (
              <button
                onClick={onLogout}
                className="ml-2 rounded-md px-2 py-1 text-xs text-ink-500 hover:bg-ink-100 hover:text-ink-900"
              >
                로그아웃
              </button>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}

function BellIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  );
}
