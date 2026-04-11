import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, KPICard, FunnelChart } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function DashboardPage() {
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'dashboard'], queryFn: pa.dashboard });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">대시보드</h1>
        <p className="mt-1 text-sm text-ink-500">미국 아마존 → 한국 구매대행 파이프라인 조감.</p>
      </header>

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <KPICard label="활성 상품"   value={data.kpis?.active_products?.toLocaleString() || 0} accent="pa" />
            <KPICard label="평균 마진"   value={`${data.kpis?.avg_margin || 0}%`} accent="pa" />
            <KPICard label="GO 대기"     value={data.todos?.go_pending || 0} accent="pa" />
            <KPICard label="업로드 대기" value={data.todos?.upload_pending || 0} accent="pa" />
            <KPICard label="미처리 CS"   value={data.todos?.cs_open || 0} accent="pa" />
          </div>

          <Card title="파이프라인 퍼널">
            <FunnelChart
              stages={(data.funnel || []).map((s) => ({ ...s, color: 'bg-brand-pa-500' }))}
            />
          </Card>
        </>
      )}
    </div>
  );
}
