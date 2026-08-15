import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge } from '@charisg/ui';
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

const RETURN_COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'order_id', label: '주문', width: '80px' },
  { key: 'reason', label: '사유' },
  { key: 'status', label: '상태', width: '100px' },
  { key: 'refund_krw', label: '환불액', width: '110px',
    render: (v) => v != null ? '₩' + Number(v).toLocaleString() : '—' },
];

export default function CsReturnsPage() {
  const cs = useQuery({ queryKey: ['pa', 'cs'], queryFn: () => pa.cs() });
  const ret = useQuery({ queryKey: ['pa', 'returns'], queryFn: pa.returns });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">CS·반품</h1>
        <p className="mt-1 text-sm text-ink-500">CS 티켓 + 반품·환불 내역.</p>
      </header>

      <Card title={`CS 티켓 (${cs.data?.length || 0})`} padded={false}>
        <DataTable columns={CS_COLS} rows={cs.data || []} rowKey={(r) => r.id} pageSize={20} />
      </Card>

      <Card title={`반품·환불 (${ret.data?.length || 0})`} padded={false}>
        <DataTable columns={RETURN_COLS} rows={ret.data || []} rowKey={(r) => r.id} pageSize={20} />
      </Card>
    </div>
  );
}
