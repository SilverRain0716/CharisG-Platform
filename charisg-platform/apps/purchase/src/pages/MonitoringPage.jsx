import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function MonitoringPage() {
  const stock = useQuery({ queryKey: ['pa', 'stock'], queryFn: pa.stockAlerts });
  const margin = useQuery({ queryKey: ['pa', 'margin-alerts'], queryFn: pa.marginAlerts });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">모니터링</h1>
        <p className="mt-1 text-sm text-ink-500">재고, 마진 변동, 경쟁가 — 자동 알림 + 수동 점검.</p>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title={`재고 알림 (${stock.data?.length || 0})`} padded={false}>
          <DataTable
            columns={[
              { key: 'product_id', label: 'PID', width: '60px' },
              { key: 'title_ko', label: '상품명' },
              { key: 'type', label: '종류', width: '120px',
                render: (v) => <StatusBadge variant="warn">{v}</StatusBadge> },
              { key: 'detected_at', label: '감지', width: '160px' },
            ]}
            rows={stock.data || []}
            rowKey={(r) => r.id}
            pageSize={20}
          />
        </Card>

        <Card title={`마진 임계 미달 (${margin.data?.length || 0})`} padded={false}>
          <DataTable
            columns={[
              { key: 'product_id', label: 'PID', width: '60px' },
              { key: 'title_ko', label: '상품명' },
              { key: 'margin_pct', label: '마진', width: '90px',
                render: (v) => v != null ? Number(v).toFixed(1) + '%' : '—' },
              { key: 'captured_at', label: '시각', width: '160px' },
            ]}
            rows={margin.data || []}
            rowKey={(r) => r.product_id + '-' + r.captured_at}
            pageSize={20}
          />
        </Card>
      </div>
    </div>
  );
}
