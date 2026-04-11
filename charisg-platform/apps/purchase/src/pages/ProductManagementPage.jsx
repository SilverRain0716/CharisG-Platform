import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'title_ko', label: '상품명' },
  { key: 'sale_price_krw', label: '판매가', sortable: true, width: '110px',
    render: (v) => v != null ? '₩' + Number(v).toLocaleString() : '—' },
  { key: 'margin_pct', label: '마진%', sortable: true, width: '80px',
    render: (v) => v != null ? Number(v).toFixed(1) + '%' : '—' },
  { key: 'status', label: '상태', width: '90px',
    render: (v) => <StatusBadge variant={v === 'active' ? 'ok' : v === 'paused' ? 'warn' : 'neutral'}>{v}</StatusBadge> },
];

export default function ProductManagementPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'products'], queryFn: () => pa.products({ limit: 200 }) });

  const generate = useMutation({ mutationFn: (pid) => pa.generateDetail(pid) });
  const upSS = useMutation({ mutationFn: (pid) => pa.uploadSmartstore(pid) });
  const upCp = useMutation({ mutationFn: (pid) => pa.uploadCoupang(pid) });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">상품 관리</h1>
        <p className="mt-1 text-sm text-ink-500">상세페이지 → 등록 → 활성 상품 라이프사이클.</p>
      </header>

      <Card title={`활성 상품 (${data?.total || 0})`} padded={false}>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={[
              ...COLS,
              {
                key: 'actions',
                label: '액션',
                width: '300px',
                render: (_, row) => (
                  <div className="flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => generate.mutate(row.id)}>상세생성</Button>
                    <Button size="sm" variant="pa" onClick={() => upSS.mutate(row.id)}>스마트</Button>
                    <Button size="sm" variant="ds" onClick={() => upCp.mutate(row.id)}>쿠팡</Button>
                  </div>
                ),
              },
            ]}
            rows={data?.items || []}
            rowKey={(r) => r.id}
          />
        )}
      </Card>
    </div>
  );
}
