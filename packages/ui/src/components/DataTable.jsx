import React, { useState, useMemo } from 'react';
import { cx } from '../utils/cx.js';

/**
 * DataTable — 배치 검토용 밀집 테이블.
 *
 * props 는 예전과 동일하다(기존 화면 다수가 쓰고 있다). 바뀐 것은 밀도와
 * 색뿐 — 행 높이를 줄이고, 색은 전부 토큰을 거치게 해서 다크에서도 그대로 산다.
 * 숫자 열은 tabular-nums 로 자릿수를 고정한다.
 *
 *   columns: [{ key, label, render?, sortable?, width?, wrap?, maxWidth?, align? }]
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
    <div className="min-w-0 overflow-hidden rounded-lg border border-line bg-surface">
      <div className="overflow-x-auto">
        <table className="min-w-full text-[12.5px]">
          <thead>
            <tr className="bg-sunken">
              {selectable && (
                <th className="w-8 border-b border-line px-2.5 py-1.5">
                  <input
                    type="checkbox"
                    aria-label="전체 선택"
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
                    'whitespace-nowrap border-b border-line px-2.5 py-1.5 text-2xs font-semibold uppercase tracking-[0.06em] text-ink-400',
                    c.align === 'right' ? 'text-right' : 'text-left',
                    c.sortable && 'cursor-pointer select-none hover:text-ink-900',
                  )}
                >
                  <span className="inline-flex items-center gap-1">
                    {c.label}
                    {c.sortable && sort?.key === c.key && <span>{sort.dir === 'asc' ? '▲' : '▼'}</span>}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
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
                <tr key={k} className="border-b border-line last:border-b-0 hover:bg-sunken">
                  {selectable && (
                    <td className="w-8 px-2.5 py-1.5">
                      <input
                        type="checkbox"
                        aria-label="행 선택"
                        checked={selected.has(k)}
                        onChange={() => toggleSelect(k)}
                      />
                    </td>
                  )}
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={cx(
                        'px-2.5 py-1.5 align-top text-ink-700',
                        c.align === 'right' && 'text-right font-mono tabular-nums',
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
        <div className="flex items-center justify-between border-t border-line bg-sunken px-3 py-1.5 text-[11.5px] text-ink-500">
          <span className="font-mono tabular-nums">총 {total.toLocaleString()}건 · {page + 1} / {pageCount}</span>
          <div className="flex gap-1">
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded border border-line bg-surface px-2 py-0.5 disabled:opacity-50"
            >
              이전
            </button>
            <button
              disabled={page >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              className="rounded border border-line bg-surface px-2 py-0.5 disabled:opacity-50"
            >
              다음
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
