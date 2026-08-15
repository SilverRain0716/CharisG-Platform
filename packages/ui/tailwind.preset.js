/**
 * Charis G 공유 Tailwind preset.
 *
 * 색은 전부 packages/ui/src/styles/tokens.css 의 CSS 변수를 참조한다.
 * "R G B" 삼중값이라 bg-ink-50/30 같은 투명도 유틸이 그대로 동작하고,
 * 다크 전환은 변수만 갈아끼우므로 컴포넌트에 dark: 를 달 필요가 없다.
 *
 * ★새 색은 tokens.css 에 먼저 추가하고 여기에 노출한다. 여기서 hex 를
 *   직접 쓰면 그 색만 다크에서 안 바뀐다.
 */

/** rgb(var(--x) / <alpha-value>) 로 감싸는 헬퍼 */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Pretendard Variable', 'Pretendard', '-apple-system', 'BlinkMacSystemFont', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        /* 표면 */
        canvas:  v('canvas'),
        surface: v('surface'),
        sunken:  v('sunken'),

        /* 경계 */
        line: {
          DEFAULT: v('line'),
          strong:  v('line-strong'),
        },

        /* 중립 램프 — 다크에서 반전된다 */
        ink: {
          50:  v('ink-50'),
          100: v('ink-100'),
          200: v('ink-200'),
          300: v('ink-300'),
          400: v('ink-400'),
          500: v('ink-500'),
          600: v('ink-600'),
          700: v('ink-700'),
          800: v('ink-800'),
          900: v('ink-900'),
        },

        /* 현재 채널 액센트 — [data-channel] 로 결정 */
        accent: {
          DEFAULT: v('accent'),
          fg:      v('accent-fg'),
        },

        /* 채널 식별색 (한 화면에 여러 채널이 같이 보일 때) */
        channel: {
          coupang:    v('ch-cp'),
          smartstore: v('ch-nv'),
          elevenst:   v('ch-st'),
          esm:        v('ch-em'),
        },

        /* 상태 — 채널색과 분리 */
        signal: {
          ok:   v('ok'),
          warn: v('warn'),
          err:  v('crit'),
          info: v('info'),
        },
        soft: {
          ok:   v('ok-soft'),
          warn: v('warn-soft'),
          err:  v('crit-soft'),
          info: v('info-soft'),
        },

        /* 구버전 호환 — brand.pa / brand.ds 를 쓰던 화면이 남아 있다.
           값은 채널색으로 흘려보내 다크에서도 깨지지 않게 한다. */
        brand: {
          shell: v('ink-900'),
          pa: {
            50:  v('sunken'),
            100: v('sunken'),
            500: v('ch-cp'),
            600: v('ch-cp'),
            700: v('ch-cp'),
            900: v('ch-cp'),
          },
          ds: {
            50:  v('sunken'),
            100: v('sunken'),
            500: v('ch-nv'),
            600: v('ch-nv'),
            700: v('ch-nv'),
            900: v('ch-nv'),
          },
        },
      },
      borderRadius: {
        DEFAULT: '5px',
        lg: '8px',
        xl: '10px',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        'card-hover': 'var(--shadow-pop)',
        pop: 'var(--shadow-pop)',
      },
      fontSize: {
        /* 고밀도 콘솔용 — 기본 스케일보다 한 칸씩 조인다 */
        '2xs': ['10.5px', { lineHeight: '1.35' }],
      },
    },
  },
};
