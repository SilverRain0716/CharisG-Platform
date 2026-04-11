import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable } from '@charisg/ui';
import { pa } from '../api/pa.js';

const COLS = [
  { key: 'keyword', label: '키워드' },
  { key: 'source', label: '소스', width: '120px' },
  { key: 'cluster_label', label: '클러스터' },
  { key: 'monthly_pc', label: 'PC',     sortable: true, width: '90px' },
  { key: 'monthly_mobile', label: 'Mobile', sortable: true, width: '100px' },
  { key: 'competition', label: '경쟁도', width: '90px' },
  { key: 'status', label: '상태', width: '100px' },
];

export default function DiscoveryPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'keywords'], queryFn: () => pa.keywords({ limit: 200 }) });
  const clusters = useQuery({ queryKey: ['pa', 'clusters'], queryFn: pa.clusters });

  const runDatalab = useMutation({ mutationFn: pa.runDatalab, onSuccess: () => setTimeout(() => qc.invalidateQueries(), 1500) });
  const runCluster = useMutation({ mutationFn: () => pa.runCluster(null), onSuccess: () => qc.invalidateQueries() });

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">디스커버리</h1>
          <p className="mt-1 text-sm text-ink-500">네이버 데이터랩 + 검색광고 + AI 클러스터링.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => runDatalab.mutate()} disabled={runDatalab.isPending}>
            데이터랩 풀 파이프라인
          </Button>
          <Button variant="pa" onClick={() => runCluster.mutate()} disabled={runCluster.isPending}>
            클러스터링 실행
          </Button>
        </div>
      </header>

      <Card title={`키워드 (${data?.total || 0})`} padded={false}>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable columns={COLS} rows={data?.items || []} rowKey={(r) => r.id} />
        )}
      </Card>

      <Card title="클러스터">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {(clusters.data || []).map((c) => (
            <div key={c.id} className="rounded-md border border-ink-200 bg-white p-3">
              <div className="text-xs text-ink-500">{c.label}</div>
              <div className="text-sm font-semibold text-ink-900">{c.representative}</div>
              <div className="text-[11px] text-ink-400">{c.member_count}개</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
