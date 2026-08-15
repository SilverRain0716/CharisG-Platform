import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

/**
 * 테마 — 'system' | 'light' | 'dark'
 *
 * 실제 색 전환은 <html data-theme> 를 바꾸는 것으로 끝난다(tokens.css).
 * 여기서는 선택값을 localStorage 에 보관하고 OS 설정 변화를 따라갈 뿐이다.
 *
 * ★첫 페인트 전에 적용해야 흰 화면이 번쩍이지 않는다. index.html 의
 *   인라인 스크립트가 같은 키를 읽어 미리 칠하고, 여기서는 그 상태를 이어받는다.
 */

const STORAGE_KEY = 'charisg_theme';
const ThemeContext = createContext(null);

function readStored() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
  } catch {
    return 'system';
  }
}

function prefersDark() {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-color-scheme: dark)').matches;
}

/** 선택값을 실제 DOM 속성으로 반영한다. system 이면 속성을 지워 미디어쿼리에 맡긴다. */
function paint(theme) {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readStored);

  useEffect(() => {
    paint(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* 사생활 보호 모드 등 — 저장 실패해도 화면은 정상 동작한다 */
    }
  }, [theme]);

  // system 일 때만 OS 변화를 따라간다. 명시 선택은 사용자 의사이므로 건드리지 않는다.
  useEffect(() => {
    if (theme !== 'system') return undefined;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => paint('system');
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [theme]);

  const setTheme = useCallback((next) => setThemeState(next), []);
  const toggle = useCallback(() => {
    setThemeState((cur) => {
      const dark = cur === 'dark' || (cur === 'system' && prefersDark());
      return dark ? 'light' : 'dark';
    });
  }, []);

  const resolved = theme === 'system' ? (prefersDark() ? 'dark' : 'light') : theme;

  return (
    <ThemeContext.Provider value={{ theme, resolved, setTheme, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    // Provider 밖에서도 터지지 않게 — 토글만 못 할 뿐 렌더는 된다.
    return { theme: 'system', resolved: 'light', setTheme: () => {}, toggle: () => {} };
  }
  return ctx;
}

/** 아이콘 하나짜리 토글. 현재 해석된 테마의 반대를 안내한다. */
export function ThemeToggle({ className }) {
  const { resolved, toggle } = useTheme();
  const next = resolved === 'dark' ? '라이트' : '다크';
  return (
    <button
      type="button"
      onClick={toggle}
      title={`${next} 모드로`}
      aria-label={`${next} 모드로 전환`}
      className={[
        'inline-flex h-7 w-7 items-center justify-center rounded text-ink-500',
        'hover:bg-sunken hover:text-ink-900',
        className || '',
      ].join(' ')}
    >
      {resolved === 'dark' ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}
