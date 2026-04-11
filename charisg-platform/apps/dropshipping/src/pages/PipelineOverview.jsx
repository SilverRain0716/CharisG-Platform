import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, KPICard, FunnelChart, AlertFeed } from '@charisg/ui';
import { ds } from '../api/ds.js';

export default function PipelineOverview() {
  const { data, isLoading } = useQuery({ queryKey: ['ds', 'dashboard'], queryFn: ds.dashboard });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">대시보드</h1>
        <p className="mt-1 text-sm text-ink-500">Charis G Amazon US FBM 드랍쉬핑 파이프라인 조감.</p>
      </header>

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <KPICard label="GO 건수"     value={data.kpis?.go_count?.toLocaleString() || 0} accent="ds" />
            <KPICard label="평균 마진"   value={`${data.kpis?.avg_margin || 0}%`}            accent="ds" />
            <KPICard label="리스팅 진행" value={data.kpis?.listing_progress || '0/0'}        accent="ds" />
            <KPICard label="활성 상품"   value={data.kpis?.active_products?.toLocaleString() || 0} accent="ds" />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <Card title="파이프라인 퍼널">
                <FunnelChart stages={data.funnel || []} />
              </Card>
            </div>
            <Card title="알림">
              <AlertFeed items={data.alerts || []} />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
