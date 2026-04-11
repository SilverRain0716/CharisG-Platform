import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge, Input } from '@charisg/ui';
import { pa } from '../api/pa.js';

const SHIPPING_VARIANT = { PASS: 'ok', WARN: 'warn', REJECT: 'err' };

const COLS = [
  { key: 'asin', label: 'ASIN', width: '110px' },
  { key: 'title', label: '상품명' },
  { key: 'price_usd', label: '$', sortable: true, width: '70px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '—' },
  { key: 'rating', label: '★', width: '50px' },
  { key: 'review_count', label: '리뷰', sortable: true, width: '80px' },
  { key: 'in_stock', label: '재고', width: '60px',
    render: (v) => v ? <StatusBadge variant="ok">in</StatusBadge> : <StatusBadge variant="err">out</StatusBadge> },
  { key: 'shipping_status', label: '배송', width: '80px',
    render: (v) => v ? <StatusBadge variant={SHIPPING_VARIANT[v] || 'neutral'}>{v}</StatusBadge> : '—' },
  { key: 'sourcing_status', label: '판단', width: '100px' },
];

export default function SourcingPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState([]);
  const [calc, setCalc] = useState({ usd: '', krw: '' });
  const [calcResult, setCalcResult] = useState(null);

  const { data, isLoading } = useQuery({ queryKey: ['pa', 'sourcing'], queryFn: () => pa.sourcing({ limit: 200 }) });

  const decide = useMutation({
    mutationFn: ({ ids, decision }) => pa.bulkDecision(ids, decision, '일괄 처리'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'sourcing'] }),
  });

  async function runCalc() {
    if (!calc.usd || !calc.krw) return;
    const r = await pa.marginCalc({
      amazon_price_usd: parseFloat(calc.usd),
      sale_price_krw: parseFloat(calc.krw),
    });
    setCalcResult(r);
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">소싱</h1>
        <p className="mt-1 text-sm text-ink-500">후보 리스트 + 마진 분석 + 통관 + GO/NO-GO.</p>
      </header>

      <Card title="배치 마진 계산기">
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <Input label="아마존 가격 (USD)" value={calc.usd} onChange={(e) => setCalc({ ...calc, usd: e.target.value })} placeholder="29.99" />
          <Input label="우리 판매가 (KRW)" value={calc.krw} onChange={(e) => setCalc({ ...calc, krw: e.target.value })} placeholder="69900" />
          <div className="flex items-end">
            <Button variant="pa" onClick={runCalc}>계산</Button>
          </div>
        </div>
        {calcResult && (
          <div className="mt-4 grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
            <div className="rounded-md bg-ink-50 p-3"><div className="text-xs text-ink-500">매입원가</div><div className="font-semibold">₩{calcResult.cost_krw?.toLocaleString()}</div></div>
            <div className="rounded-md bg-ink-50 p-3"><div className="text-xs text-ink-500">우리 순익</div><div className="font-semibold text-emerald-700">₩{calcResult.seller_net_krw?.toLocaleString()}</div></div>
            <div className="rounded-md bg-ink-50 p-3"><div className="text-xs text-ink-500">마진율</div><div className="font-semibold">{calcResult.seller_margin_pct}%</div></div>
            <div className="rounded-md bg-ink-50 p-3"><div className="text-xs text-ink-500">고객 총 비용</div><div className="font-semibold">₩{calcResult.customer_total_krw?.toLocaleString()}</div></div>
          </div>
        )}
      </Card>

      <Card
        title={`소싱 후보 (${data?.total || 0})`}
        padded={false}
        action={
          <div className="flex gap-2 px-4">
            <Button variant="ds" size="sm" onClick={() => decide.mutate({ ids: selected, decision: 'go' })} disabled={!selected.length}>
              GO 일괄
            </Button>
            <Button variant="danger" size="sm" onClick={() => decide.mutate({ ids: selected, decision: 'nogo' })} disabled={!selected.length}>
              NO-GO 일괄
            </Button>
          </div>
        }
      >
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={COLS}
            rows={data?.items || []}
            rowKey={(r) => r.id}
            selectable
            onSelect={setSelected}
          />
        )}
      </Card>
    </div>
  );
}
