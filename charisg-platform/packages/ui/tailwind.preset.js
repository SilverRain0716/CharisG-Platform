/**
 * Charis G Platform 공유 Tailwind preset.
 * 3개 앱(Hub/DS/PA)이 동일 디자인 시스템을 사용한다.
 *
 * 컬러 전략:
 *   - shell/hub: slate (중립)
 *   - dropshipping: teal
 *   - purchase agent: violet
 */
export default {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Pretendard Variable', 'Pretendard', '-apple-system', 'BlinkMacSystemFont', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        brand: {
          shell: '#0f172a',
          ds: {
            50:  '#f0fdfa',
            100: '#ccfbf1',
            500: '#14b8a6',
            600: '#0d9488',
            700: '#0f766e',
            900: '#134e4a',
          },
          pa: {
            50:  '#f5f3ff',
            100: '#ede9fe',
            500: '#8b5cf6',
            600: '#7c3aed',
            700: '#6d28d9',
            900: '#4c1d95',
          },
        },
        ink: {
          50:  '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
        },
        signal: {
          ok:   '#10b981',
          warn: '#f59e0b',
          err:  '#ef4444',
          info: '#3b82f6',
        },
      },
      borderRadius: {
        DEFAULT: '8px',
        lg: '12px',
        xl: '16px',
      },
      boxShadow: {
        card: '0 1px 3px 0 rgb(15 23 42 / 0.06), 0 1px 2px -1px rgb(15 23 42 / 0.06)',
        'card-hover': '0 4px 12px -2px rgb(15 23 42 / 0.10), 0 2px 4px -2px rgb(15 23 42 / 0.06)',
      },
    },
  },
};
