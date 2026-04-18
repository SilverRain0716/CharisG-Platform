import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge, KPICard } from '@charisg/ui';
import { ds } from '../api/ds.js';

const COLS = [
  { key: 'asin', label: 'ASIN', width: '130px',
    render: (v) => v ? <span className="font-mono text-xs">{v}</span> : '\u2014' },
  { key: 'sku', label: 'SKU', width: '110px',
    render: (v) => v ? <span className="font-mono text-xs">{v}</span> : '\u2014' },
  { key: 'product_name', label: '상품명', maxWidth: '280px', wrap: true },
  { key: 'current_price', label: '현재가', sortable: true, width: '80px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '\u2014' },
  { key: 'current_stock', label: '재고', sortable: true, width: '60px',
    render: (v) => v ?? '—' },
  { key: 'status', label: '상태', width: '90px',
    render: (v) => {
      const variant = v === 'active' ? 'ok' : v === 'paused' ? 'warn' : v === 'listed' ? 'info' : 'neutral';
      return <StatusBadge variant={variant}>{v || '—'}</StatusBadge>;
    }},
  { key: 'real_margin_pct', label: '마진%', sortable: true, width: '70px',
    render: (v) => v != null ? Number(v).toFixed(1) + '%' : '—' },
  { key: 'listed_at', label: '등록일', width: '100px',
    render: (v) => v ? v.slice(0, 10) : '—' },
];

export default function MyListings() {
  const { data, isLoading } = useQuery({
    queryKey: ['ds', 'listings'],
    queryFn: ds.listings,
  });

  const kpis = data?.kpis || {};

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">내 리스팅</h1>
        <p className="mt-1 text-sm text-ink-500">Amazon에 등록된 상품 모니터링.</p>
      </header>

      <div className="grid grid-cols-3 gap-4">
        <KPICard label="활성" value={kpis.active ?? 0} accent="ds" />
        <KPICard label="일시정지" value={kpis.paused ?? 0} accent="ds" />
        <KPICard label="총 등록" value={kpis.total ?? 0} accent="ds" />
      </div>

      <Card padded={false}>
        {isLoading ? (
          <div className="p-10 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={COLS}
            rows={data?.items || []}
            rowKey={(r) => r.id}
            defaultSort={{ key: 'listed_at', dir: 'desc' }}
            emptyText="Amazon에 등록된 상품이 없습니다. 상품 후보 → 업로드 대기 탭에서 등록하세요."
          />
        )}
      </Card>
    </div>
  );
}
