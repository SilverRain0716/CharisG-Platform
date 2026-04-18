import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, KPICard, Button, Input, StatusBadge } from '@charisg/ui';
import { useMarket } from '../App.jsx';

const TARGETS = {
  odr: { max: 1.0, target: 0.5, label: 'ODR' },
  late_shipment_rate: { max: 4.0, target: 2.0, label: 'LSR' },
  cancel_rate: { max: 2.5, target: 1.0, label: 'Cancel' },
  valid_tracking_rate: { min: 95, target: 99, label: 'VTR' },
};

function gauge(metric, value) {
  const t = TARGETS[metric];
  if (value == null) return { variant: 'neutral', label: '—' };
  if (metric === 'valid_tracking_rate') {
    if (value >= t.target) return { variant: 'ok', label: '안전' };
    if (value >= t.min) return { variant: 'warn', label: '주의' };
    return { variant: 'err', label: '위험' };
  }
  if (value <= t.target) return { variant: 'ok', label: '안전' };
  if (value <= t.max) return { variant: 'warn', label: '주의' };
  return { variant: 'err', label: '위험' };
}

export default function AccountHealth() {
  const { market, ds } = useMarket();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['ds', 'health', market], queryFn: ds.health });

  const [form, setForm] = useState({ odr: '', late_shipment_rate: '', cancel_rate: '', valid_tracking_rate: '', note: '' });

  const post = useMutation({
    mutationFn: () =>
      ds.postHealth({
        odr: form.odr ? parseFloat(form.odr) : null,
        late_shipment_rate: form.late_shipment_rate ? parseFloat(form.late_shipment_rate) : null,
        cancel_rate: form.cancel_rate ? parseFloat(form.cancel_rate) : null,
        valid_tracking_rate: form.valid_tracking_rate ? parseFloat(form.valid_tracking_rate) : null,
        note: form.note,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'health'] }),
  });

  const cur = data?.current || {};

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">계정 건강도</h1>
        <p className="mt-1 text-sm text-ink-500">
          Phase 0 — 수동 입력 모드 (SP-API 미연동). 아마존 셀러 센트럴 값을 직접 입력.
        </p>
      </header>

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {!isLoading && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            {Object.keys(TARGETS).map((m) => {
              const v = cur[m];
              const g = gauge(m, v);
              return (
                <div key={m} className="rounded-lg bg-white p-5 shadow-card ring-1 ring-ink-100">
                  <div className="flex items-center justify-between text-xs font-semibold uppercase text-ink-500">
                    {TARGETS[m].label}
                    <StatusBadge variant={g.variant}>{g.label}</StatusBadge>
                  </div>
                  <div className="mt-2 text-2xl font-semibold text-ink-900">{v != null ? v + '%' : '—'}</div>
                  <div className="text-xs text-ink-400">목표 {TARGETS[m].target}%</div>
                </div>
              );
            })}
          </div>

          <Card title="수동 입력">
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
              <Input label="ODR (%)" value={form.odr}                onChange={(e) => setForm({ ...form, odr: e.target.value })} />
              <Input label="LSR (%)" value={form.late_shipment_rate} onChange={(e) => setForm({ ...form, late_shipment_rate: e.target.value })} />
              <Input label="Cancel (%)" value={form.cancel_rate}     onChange={(e) => setForm({ ...form, cancel_rate: e.target.value })} />
              <Input label="VTR (%)" value={form.valid_tracking_rate} onChange={(e) => setForm({ ...form, valid_tracking_rate: e.target.value })} />
              <Input label="메모"   value={form.note}                onChange={(e) => setForm({ ...form, note: e.target.value })} />
            </div>
            <div className="mt-4 flex justify-end">
              <Button variant="ds" onClick={() => post.mutate()} disabled={post.isPending}>
                {post.isPending ? '저장 중...' : '저장'}
              </Button>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
