import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * MetricStrip / Metric — 참고 지표 한 줄.
 *
 * 카드 그림자 대신 1px 헤어라인으로만 칸을 나눈다. 같은 세로 공간에 기존
 * KPICard 세 장 분량이 들어간다. 액션이 필요한 항목은 여기가 아니라
 * TriageQueue 로 간다 — 이 줄은 "참고"만 담당한다.
 *
 * <MetricStrip>
 *   <Metric label="등록 상품" value="9,812" delta={+128} deltaLabel="7일" />
 * </MetricStrip>
 */
export function MetricStrip({ children, cols, className }) {
  const n = cols || React.Children.count(children) || 1;
  return (
    <section
      className={cx('grid overflow-hidden rounded-lg border border-line bg-surface', className)}
      style={{ gridTemplateColumns: `repeat(${n}, minmax(0, 1fr))` }}
    >
      {children}
    </section>
  );
}

export function Metric({ label, value, unit, delta, deltaLabel, hint, tone, split, children }) {
  const dir = delta == null ? null : delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
  return (
    <div className="min-w-0 border-l border-line px-3 py-2.5 first:border-l-0">
      <div className="text-[11px] font-medium text-ink-400">{label}</div>
      <div
        className={cx(
          'mt-0.5 flex items-baseline gap-0.5 font-mono text-[22px] font-semibold tracking-tight tabular-nums',
          tone === 'crit' ? 'text-signal-err' : tone === 'warn' ? 'text-signal-warn' : 'text-ink-900',
        )}
      >
        {value}
        {unit && <span className="text-[13px] font-medium text-ink-500">{unit}</span>}
      </div>

      <div className="mt-1 flex min-h-[18px] items-center gap-1.5">
        {dir && (
          <span
            className={cx(
              'font-mono text-[11px] font-semibold tabular-nums',
              dir === 'up' ? 'text-signal-ok' : dir === 'down' ? 'text-signal-err' : 'text-ink-400',
            )}
          >
            {delta > 0 ? '+' : ''}{typeof delta === 'number' ? delta.toLocaleString() : delta}
          </span>
        )}
        {deltaLabel && <span className="text-[11px] text-ink-400">{deltaLabel}</span>}
        {hint && <span className="text-[11px] text-ink-400">{hint}</span>}
        {children}
      </div>

      {/* split: [{ value, className }] — 합계를 채널·계정별로 쪼개 보여주는 얇은 띠 */}
      {split && split.length > 0 && (
        <div className="mt-1.5 flex h-[5px] w-full gap-0.5">
          {split.map((s, i) => (
            <i
              key={i}
              title={s.title}
              className={cx('block rounded-[1px]', s.className || 'bg-ink-300')}
              style={{ width: `${s.pct}%` }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
