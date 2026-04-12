import React, { useState, useMemo } from 'react';
import { cx } from '../utils/cx.js';

/**
 * DataTable — 30~100건 배치 검토용 데이터 테이블.
 *
 * Props:
 *   columns:    [{ key, label, render?, sortable?, width?, wrap?, maxWidth? }]
 *     wrap:     true 면 해당 셀만 줄바꿈 허용 (기본 nowrap)
 *     maxWidth: 인라인 style.maxWidth (예: '320px') — wrap 과 함께 써서 상한 지정
 *   rows:       any[]
 *   rowKey:     (row) => string
 *   selectable: boolean
 *   onSelect:   (selectedKeys) => void
 *   defaultSort: { key, dir }    // dir: 'asc' | 'desc'
 *   pageSize:   number  (기본 50)
 *   emptyText:  string
 */
export function DataTable({
  columns,
  rows = [],
  rowKey = (r) => r.id,
  selectable = false,
  onSelect,
  defaultSort,
  pageSize = 50,
  emptyText = '데이터 없음',
}) {
  const [sort, setSort] = useState(defaultSort || null);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState(new Set());

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const arr = [...rows];
    arr.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      if (av == null) return 1;
      if (bv == null) return -1;
      if (av < bv) return sort.dir === 'asc' ? -1 : 1;
      if (av > bv) return sort.dir === 'asc' ? 1 : -1;
      return 0;
    });
    return arr;
  }, [rows, sort]);

  const total = sorted.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const pageRows = sorted.slice(page * pageSize, (page + 1) * pageSize);

  function toggleSort(key) {
    setSort((s) => {
      if (!s || s.key !== key) return { key, dir: 'desc' };
      if (s.dir === 'desc') return { key, dir: 'asc' };
      return null;
    });
  }

  function toggleSelect(key) {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setSelected(next);
    onSelect && onSelect(Array.from(next));
  }

  function toggleAll() {
    if (selected.size === pageRows.length) {
      setSelected(new Set());
      onSelect && onSelect([]);
    } else {
      const next = new Set(pageRows.map(rowKey));
      setSelected(next);
      onSelect && onSelect(Array.from(next));
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-ink-200 bg-white min-w-0">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-ink-200 text-sm">
          <thead className="bg-ink-50">
            <tr>
              {selectable && (
                <th className="w-10 px-3 py-2.5">
                  <input
                    type="checkbox"
                    checked={pageRows.length > 0 && selected.size === pageRows.length}
                    onChange={toggleAll}
                  />
                </th>
              )}
              {columns.map((c) => (
                <th
                  key={c.key}
                  style={{ width: c.width }}
                  onClick={() => c.sortable && toggleSort(c.key)}
                  className={cx(
                    'px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-500',
                    c.sortable && 'cursor-pointer select-none hover:text-ink-900',
                  )}
                >
                  <span className="inline-flex items-center gap-1">
                    {c.label}
                    {c.sortable && sort?.key === c.key && (
                      <span>{sort.dir === 'asc' ? '▲' : '▼'}</span>
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-100 bg-white">
            {pageRows.length === 0 && (
              <tr>
                <td colSpan={columns.length + (selectable ? 1 : 0)} className="px-3 py-10 text-center text-ink-400">
                  {emptyText}
                </td>
              </tr>
            )}
            {pageRows.map((row) => {
              const k = rowKey(row);
              return (
                <tr key={k} className="hover:bg-ink-50">
                  {selectable && (
                    <td className="w-10 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selected.has(k)}
                        onChange={() => toggleSelect(k)}
                      />
                    </td>
                  )}
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={cx(
                        'px-3 py-2 align-top text-ink-700',
                        c.wrap ? 'whitespace-normal break-words' : 'whitespace-nowrap',
                      )}
                      style={c.maxWidth ? { maxWidth: c.maxWidth } : undefined}
                    >
                      {c.render ? c.render(row[c.key], row) : row[c.key]}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className="flex items-center justify-between border-t border-ink-200 bg-ink-50 px-3 py-2 text-xs text-ink-500">
          <span>총 {total}건 · {page + 1} / {pageCount}</span>
          <div className="flex gap-1">
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded border border-ink-200 bg-white px-2 py-1 disabled:opacity-50"
            >
              이전
            </button>
            <button
              disabled={page >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              className="rounded border border-ink-200 bg-white px-2 py-1 disabled:opacity-50"
            >
              다음
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
