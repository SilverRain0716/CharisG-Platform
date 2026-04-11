import React from 'react';
import { cx } from '../utils/cx.js';

/**
 * KanbanBoard — 단순 칸반 보드.
 *
 * Props:
 *   columns: [{ id, label, color?, items: [{id, ...}] }]
 *   renderCard: (item) => ReactNode
 *   onMove: (itemId, fromColId, toColId) => void
 */
export function KanbanBoard({ columns = [], renderCard, onMove }) {
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
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
      {columns.map((col) => (
        <div
          key={col.id}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => onDrop(e, col.id)}
          className="flex min-h-[200px] flex-col rounded-lg bg-ink-50 p-3 ring-1 ring-ink-100"
        >
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-semibold text-ink-700">{col.label}</div>
            <div className="text-xs text-ink-500">{col.items?.length || 0}</div>
          </div>
          <div className="flex-1 space-y-2">
            {(col.items || []).map((it) => (
              <div
                key={it.id}
                draggable
                onDragStart={(e) => onDragStart(e, it.id, col.id)}
                className={cx(
                  'cursor-move rounded-md bg-white p-3 text-sm shadow-card ring-1 ring-ink-100 hover:shadow-card-hover',
                )}
              >
                {renderCard ? renderCard(it) : it.title}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
