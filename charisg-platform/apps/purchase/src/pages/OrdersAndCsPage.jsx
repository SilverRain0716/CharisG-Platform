import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, KanbanBoard, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const CS_COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'channel', label: '채널', width: '100px' },
  { key: 'type', label: '유형', width: '110px' },
  { key: 'priority', label: '우선', width: '70px' },
  { key: 'status', label: '상태', width: '90px',
    render: (v) => <StatusBadge variant={v === 'open' ? 'warn' : v === 'resolved' ? 'ok' : 'neutral'}>{v}</StatusBadge> },
  { key: 'customer_message', label: '메시지' },
  { key: 'created_at', label: '접수', width: '160px' },
];

export default function OrdersAndCsPage() {
  const qc = useQueryClient();
  const kanban = useQuery({ queryKey: ['pa', 'orders', 'kanban'], queryFn: pa.ordersKanban });
  const cs = useQuery({ queryKey: ['pa', 'cs'], queryFn: () => pa.cs() });
  const ret = useQuery({ queryKey: ['pa', 'returns'], queryFn: pa.returns });

  const advance = useMutation({
    mutationFn: ({ id, step }) => pa.advance(id, step, '드래그 이동'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'orders'] }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">주문·CS</h1>
        <p className="mt-1 text-sm text-ink-500">6단계 주문 칸반 + CS 티켓 + 반품·환불.</p>
      </header>

      <Card title="주문 진행 (6단계)" padded={false}>
        <div className="p-5">
          {kanban.isLoading ? (
            <div className="text-sm text-ink-400">로딩 중...</div>
          ) : (
            <KanbanBoard
              columns={kanban.data || []}
              renderCard={(item) => (
                <div className="space-y-1">
                  <div className="text-xs font-medium text-ink-900">{item.customer_name || '—'}</div>
                  <div className="text-[11px] text-ink-500">{item.channel} · ₩{item.sale_price_krw?.toLocaleString() || 0}</div>
                  <div className="text-[10px] text-ink-400">{item.placed_at?.slice(0, 10)}</div>
                </div>
              )}
              onMove={(id, _from, to) => advance.mutate({ id, step: to })}
            />
          )}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title={`CS 티켓 (${cs.data?.length || 0})`} padded={false}>
          <DataTable columns={CS_COLS} rows={cs.data || []} rowKey={(r) => r.id} pageSize={20} />
        </Card>
        <Card title={`반품·환불 (${ret.data?.length || 0})`} padded={false}>
          <DataTable
            columns={[
              { key: 'id', label: 'ID', width: '60px' },
              { key: 'order_id', label: '주문', width: '80px' },
              { key: 'reason', label: '사유' },
              { key: 'status', label: '상태', width: '100px' },
              { key: 'refund_krw', label: '환불액', width: '110px',
                render: (v) => v != null ? '₩' + Number(v).toLocaleString() : '—' },
            ]}
            rows={ret.data || []}
            rowKey={(r) => r.id}
            pageSize={20}
          />
        </Card>
      </div>
    </div>
  );
}
