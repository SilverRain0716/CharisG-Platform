import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * Card — 콘텐츠 패널.
 *
 * 그림자를 쓰지 않고 1px 헤어라인과 배경 단차로만 구획한다. 그림자가 겹치면
 * 고밀도 화면에서 시각적 잡음이 되고, 다크에서는 아예 보이지도 않는다.
 */
export function Card({ title, sub, action, children, className, padded = true, ...rest }) {
  return (
    <section className={cx('rounded-lg border border-line bg-surface', className)} {...rest}>
      {(title || action || sub) && (
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          {title && <h3 className="text-[12.5px] font-semibold text-ink-900">{title}</h3>}
          {sub && <span className="text-[11.5px] text-ink-400">{sub}</span>}
          {action && <div className="ml-auto flex items-center gap-1.5">{action}</div>}
        </header>
      )}
      <div className={padded ? 'p-3' : ''}>{children}</div>
    </section>
  );
}
