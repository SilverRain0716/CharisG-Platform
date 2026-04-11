import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge, Button } from '@charisg/ui';
import { ds } from '../api/ds.js';

const COLS = [
  { key: 'id',                label: 'ID',     sortable: true, width: '60px' },
  { key: 'product_name',      label: '상품명' },
  { key: 'amazon_category',   label: '카테고리', width: '160px' },
  { key: 'source_price',      label: 'CJ',     sortable: true, width: '70px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '—' },
  { key: 'amazon_price_p75',  label: 'A.p75',  sortable: true, width: '80px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '—' },
  { key: 'real_margin_pct',   label: '실질%',  sortable: true, width: '70px',
    render: (v) => v != null ? Number(v).toFixed(1) + '%' : '—' },
  { key: 'demand_grade',      label: 'D',      width: '40px' },
  { key: 'gap_score',         label: 'G',      width: '50px',
    render: (v) => v != null ? Number(v).toFixed(2) : '—' },
  { key: 'margin_grade',      label: 'M',      width: '40px' },
  { key: 'matrix_group',      label: 'Mat',    width: '50px' },
  { key: 'sort_score',        label: 'Sort',   sortable: true, width: '70px',
    render: (v) => v != null ? Number(v).toFixed(3) : '—' },
  { key: 'go_decision',       label: 'GO',     width: '90px',
    render: (v) => v ? <StatusBadge variant={v === 'GO' ? 'ok' : v === 'GO_ORGANIC' ? 'info' : 'err'}>{v}</StatusBadge> : '—' },
  { key: 'status',            label: '상태',    width: '90px' },
];

export default function ProductList() {
  const [filterGo, setFilterGo] = useState('');
  const [selected, setSelected] = useState([]);

  const { data, isLoading } = useQuery({
    queryKey: ['ds', 'products', filterGo],
    queryFn: () => ds.products(filterGo ? { go: filterGo } : {}),
  });

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">상품 목록</h1>
          <p className="mt-1 text-sm text-ink-500">{data?.total || 0}건 — sort_score 내림차순</p>
        </div>
        <div className="flex gap-2">
          <select
            value={filterGo}
            onChange={(e) => setFilterGo(e.target.value)}
            className="h-9 rounded-md border border-ink-200 bg-white px-3 text-sm"
          >
            <option value="">전체</option>
            <option value="GO">GO</option>
            <option value="GO_ORGANIC">GO_ORGANIC</option>
            <option value="SKIP">SKIP</option>
          </select>
          <Button variant="secondary" disabled={selected.length === 0}>
            선택 {selected.length}건 → CSV
          </Button>
        </div>
      </header>

      <Card padded={false}>
        {isLoading ? (
          <div className="p-10 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={COLS}
            rows={data?.items || []}
            rowKey={(r) => r.id}
            selectable
            onSelect={setSelected}
            defaultSort={{ key: 'sort_score', dir: 'desc' }}
          />
        )}
      </Card>
    </div>
  );
}
