import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * TriageQueue — "지금 처리할 일".
 *
 * 대시보드가 KPI 카드만 늘어놓으면 무엇이 급한지를 사람이 매번 읽어서
 * 판단해야 한다. 액션이 필요한 항목은 참고 지표와 분리해 맨 위에 두고,
 * 심각도를 왼쪽 레일(색)과 숫자 색으로 이중 표시한다.
 *
 * 각 줄은 링크다. 누르면 해당 화면이 그 조건으로 걸린 채 열려야 한다 —
 * 여기서 숫자만 보여주고 사용자가 필터를 다시 찾아 걸게 하면 의미가 없다.
 *
 * Props:
 *   items: [{ id, severity: 'crit'|'warn'|'info', label, desc, count, action, href, onClick, tag }]
 *   title, total, hint
 *   emptyText
 */
const RAIL = { crit: 'bg-signal-err', warn: 'bg-signal-warn', info: 'bg-signal-info' };
const NUM  = { crit: 'text-signal-err', warn: 'text-signal-warn', info: 'text-signal-info' };

export function TriageQueue({
  items = [],
  title = '지금 처리할 일',
  total,
  hint,
  emptyText = '처리할 일이 없습니다',
}) {
  return (
    <section className="overflow-hidden rounded-lg border border-line bg-surface">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <h3 className="text-[12.5px] font-semibold text-ink-900">{title}</h3>
        {total != null && (
          <span className="rounded-full border border-signal-err/30 bg-soft-err px-2 font-mono text-[11px] text-signal-err">
            {typeof total === 'number' ? `${total.toLocaleString()}건` : total}
          </span>
        )}
        {hint && <span className="text-[11.5px] text-ink-400">{hint}</span>}
      </header>

      {items.length === 0 ? (
        <div className="px-3 py-6 text-center text-[12.5px] text-ink-400">{emptyText}</div>
      ) : (
        <div>
          {items.map((it) => {
            const sev = it.severity || 'info';
            const Row = it.href ? 'a' : 'button';
            return (
              <Row
                key={it.id}
                href={it.href}
                type={it.href ? undefined : 'button'}
                onClick={it.onClick}
                className="grid w-full grid-cols-[3px_1fr_auto_auto] items-center gap-3 border-t border-line text-left first:border-t-0 hover:bg-sunken"
              >
                <span className={cx('self-stretch', RAIL[sev])} />
                <span className="flex flex-wrap items-center gap-2 py-2">
                  <span className="flex items-center gap-1.5 text-[13px] font-semibold text-ink-900">
                    {it.icon}
                    {it.label}
                  </span>
                  {it.tag && (
                    <span className="rounded border border-line-strong bg-sunken px-1.5 font-mono text-2xs text-ink-500">
                      {it.tag}
                    </span>
                  )}
                  {it.desc && <span className="text-[11.5px] text-ink-400">{it.desc}</span>}
                </span>
                <span className={cx('font-mono text-[15px] font-semibold tabular-nums', NUM[sev])}>
                  {typeof it.count === 'number' ? it.count.toLocaleString() : it.count}
                </span>
                <span className="whitespace-nowrap pr-3 text-[11.5px] text-ink-400">
                  {it.action || '열기'} →
                </span>
              </Row>
            );
          })}
        </div>
      )}
    </section>
  );
}
