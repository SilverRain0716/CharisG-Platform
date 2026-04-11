import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, KanbanBoard, StatusBadge } from '@charisg/ui';
import { ds } from '../api/ds.js';

export default function ListingStatus() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['ds', 'kanban'], queryFn: ds.kanban });

  const move = useMutation({
    mutationFn: ({ id, status }) => ds.setStatus(id, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'kanban'] }),
  });

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">리스팅 상태</h1>
        <p className="mt-1 text-sm text-ink-500">Candidate → Listed → Active → Paused. 드래그로 상태 변경.</p>
      </header>

      <Card padded={false}>
        <div className="p-5">
          {isLoading ? (
            <div className="text-sm text-ink-400">로딩 중...</div>
          ) : (
            <KanbanBoard
              columns={data || []}
              renderCard={(item) => (
                <div className="space-y-1">
                  <div className="line-clamp-2 text-xs font-medium text-ink-900">{item.product_name}</div>
                  <div className="flex items-center justify-between text-[11px] text-ink-500">
                    <span>{item.real_margin_pct ? Number(item.real_margin_pct).toFixed(1) + '%' : '—'}</span>
                    {item.matrix_group && <StatusBadge variant="info">{item.matrix_group}</StatusBadge>}
                  </div>
                </div>
              )}
              onMove={(id, _from, to) => move.mutate({ id, status: to })}
            />
          )}
        </div>
      </Card>
    </div>
  );
}
