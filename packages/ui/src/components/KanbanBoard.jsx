import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * KanbanBoard — 단순 칸반 보드.
 *
 * Props:
 *   columns: [{ id, label, color?, items: [{id, ...}] }]
 *   renderCard: (item) => ReactNode
 *   onMove: (itemId, fromColId, toColId) => void
 *   onCardClick: (item, colId) => void  // draggable wrapper 가 자식 onClick 을 swallow 하므로 wrapper 레벨에서 처리
 *
 * 모바일(< lg)/데스크탑(lg+) 분기는 순수 CSS 미디어 쿼리로 — JS matchMedia +
 * useEffect 는 첫 렌더 깜빡임 / 타이밍 이슈가 있어 두 변형을 모두 그리고
 * `hidden lg:block` / `lg:hidden` 으로 표시 제어. inline style 의 동적 N
 * 컬럼은 데스크탑 쪽에만 적용됨.
 */
export function KanbanBoard({ columns = [], renderCard, onMove, onCardClick }) {
  function onDragStart(e, itemId, fromCol) {
    e.dataTransfer.setData('text/plain', JSON.stringify({ itemId, fromCol }));
    e.dataTransfer.effectAllowed = 'move';
  }
  function onDrop(e, toCol) {
    e.preventDefault();
    try {
      const { itemId, fromCol } = JSON.parse(e.dataTransfer.getData('text/plain'));
      if (toCol !== fromCol && onMove) onMove(itemId, fromCol, toCol);
    } catch {}
  }

  const renderColumn = (col) => (
    <div
      key={col.id}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => onDrop(e, col.id)}
      className="flex min-h-[120px] min-w-0 flex-col rounded-lg bg-ink-50 p-3 ring-1 ring-ink-100"
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold text-ink-700">{col.label}</div>
        <div className="text-xs text-ink-500">{col.items?.length || 0}</div>
      </div>
      {(col.items?.length || 0) > 0 && (
        <div className="flex-1 space-y-2">
          {(col.items || []).map((it) => (
            <div
              key={`${col.id}-${it.id}`}
              draggable
              onDragStart={(e) => onDragStart(e, it.id, col.id)}
              onClick={onCardClick ? () => onCardClick(it, col.id) : undefined}
              className={cx(
                'break-words rounded-md bg-surface p-3 text-sm shadow-card ring-1 ring-ink-100 hover:shadow-card-hover',
                onCardClick ? 'cursor-pointer' : 'cursor-move',
              )}
            >
              {renderCard ? renderCard(it) : it.title}
            </div>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <>
      {/* 모바일: 세로 1열 적층 */}
      <div className="grid grid-cols-1 gap-4 lg:hidden">
        {columns.map(renderColumn)}
      </div>
      {/* 데스크탑: 기존 가로 N컬럼 + 가로 스크롤 */}
      <div className="-mx-2 hidden overflow-x-auto px-2 pb-2 lg:block">
        <div
          className="grid gap-4"
          style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(220px, 1fr))` }}
        >
          {columns.map(renderColumn)}
        </div>
      </div>
    </>
  );
}
