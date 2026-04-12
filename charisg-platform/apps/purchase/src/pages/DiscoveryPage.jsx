import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const STAGES = [
  { key: 'categories', label: '카테고리 확인' },
  { key: 'rank',       label: '데이터랩 수집' },
  { key: 'searchad',   label: '검색량 보강' },
  { key: 'trend',      label: '트렌드 점수' },
  { key: 'cluster',    label: '클러스터링' },
];

const STAGE_ORDER = ['init', 'categories', 'rank', 'searchad', 'trend', 'cluster', 'done'];

function stageLogAsObject(raw) {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try { return JSON.parse(raw); } catch { return {}; }
}

function stagePhase(currentStage, stageKey) {
  const ci = STAGE_ORDER.indexOf(currentStage || 'init');
  const si = STAGE_ORDER.indexOf(stageKey);
  if (ci > si) return 'done';
  if (ci === si) return 'active';
  return 'pending';
}

function stageDetail(stageKey, log) {
  const entry = log?.[stageKey];
  if (!entry) return null;
  if (stageKey === 'categories') {
    return entry.tracked != null ? `추적 ${entry.tracked}개` : null;
  }
  if (stageKey === 'rank') {
    const base = `${entry.current ?? 0}/${entry.total ?? 0}`;
    return entry.category ? `${base} (${entry.category}) · ${entry.collected ?? 0}개 수집` : base;
  }
  if (stageKey === 'searchad' || stageKey === 'trend') {
    return `${entry.current ?? 0}/${entry.total ?? 0}`;
  }
  if (stageKey === 'cluster') {
    return `입력 ${entry.input ?? 0}개 → ${entry.clusters ?? 0}개 클러스터`;
  }
  return null;
}

const COLS = [
  { key: 'keyword', label: '키워드' },
  { key: 'source', label: '소스', width: '120px' },
  { key: 'cluster_label', label: '클러스터' },
  { key: 'monthly_pc', label: 'PC', sortable: true, width: '90px',
    render: (v) => v == null ? '—' : v.toLocaleString() },
  { key: 'monthly_mobile', label: 'Mobile', sortable: true, width: '100px',
    render: (v) => v == null ? '—' : v.toLocaleString() },
  { key: 'competition', label: '경쟁도', width: '90px',
    render: (v) => v == null ? '—' : v.toFixed(1) },
  { key: 'trend_score', label: '트렌드', sortable: true, width: '100px',
    render: (v) => {
      if (v == null) return '—';
      const arrow = v >= 1 ? '▲' : '▼';
      const color = v >= 1 ? 'text-signal-ok' : 'text-signal-err';
      return <span className={color}>{arrow} {v.toFixed(2)}</span>;
    } },
  { key: 'status', label: '상태', width: '100px' },
];

export default function DiscoveryPage() {
  const qc = useQueryClient();
  const keywordsQ = useQuery({ queryKey: ['pa', 'keywords'], queryFn: () => pa.keywords({ limit: 200 }) });
  const clustersQ = useQuery({ queryKey: ['pa', 'clusters'], queryFn: pa.clusters });

  const statusQ = useQuery({
    queryKey: ['pa', 'discoveryStatus'],
    queryFn: pa.discoveryStatus,
    refetchInterval: (query) => {
      const data = query?.state?.data;
      return data?.status === 'running' ? 2000 : false;
    },
  });

  const run = statusQ.data;
  const isRunning = run?.status === 'running';
  const log = stageLogAsObject(run?.stage_log);
  const currentStage = run?.current_stage || 'init';

  // 실행 종료 감지 → 키워드/클러스터 invalidate
  const prevRunning = React.useRef(isRunning);
  React.useEffect(() => {
    if (prevRunning.current && !isRunning) {
      qc.invalidateQueries({ queryKey: ['pa', 'keywords'] });
      qc.invalidateQueries({ queryKey: ['pa', 'clusters'] });
    }
    prevRunning.current = isRunning;
  }, [isRunning, qc]);

  const runMut = useMutation({
    mutationFn: pa.discoveryRun,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'discoveryStatus'] }),
  });
  const clusterMut = useMutation({
    mutationFn: () => pa.runCluster(null),
    onSuccess: () => qc.invalidateQueries(),
  });

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">디스커버리</h1>
          <p className="mt-1 text-sm text-ink-500">
            네이버 데이터랩 카테고리 → 인기 키워드 → 검색량 → 트렌드 → AI 클러스터링.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="pa"
            onClick={() => runMut.mutate()}
            disabled={isRunning || runMut.isPending}
          >
            {isRunning ? '실행 중…' : '디스커버리 실행'}
          </Button>
        </div>
      </header>

      <Card title="파이프라인 상태" action={run && (
        <StatusBadge variant={run.status === 'done' ? 'ok' : run.status === 'failed' ? 'err' : 'warn'}>
          {run.status}
        </StatusBadge>
      )}>
        {!run && (
          <div className="text-sm text-ink-400">실행 이력 없음. "디스커버리 실행" 버튼을 눌러 시작하세요.</div>
        )}
        {run && (
          <>
            <ol className="flex flex-wrap gap-2">
              {STAGES.map((s, i) => {
                const phase = stagePhase(currentStage, s.key);
                const isErr = run.status === 'failed' && phase === 'active';
                const detail = stageDetail(s.key, log);
                return (
                  <li
                    key={s.key}
                    className={
                      'flex min-w-[140px] flex-1 flex-col rounded-md border px-3 py-2 ' +
                      (isErr
                        ? 'border-signal-err bg-red-50'
                        : phase === 'done'
                        ? 'border-signal-ok bg-green-50'
                        : phase === 'active'
                        ? 'border-brand-pa-600 bg-brand-pa-50'
                        : 'border-ink-200 bg-ink-50')
                    }
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-ink-500">STEP {i + 1}</span>
                      {phase === 'active' && <span className="h-2 w-2 animate-pulse rounded-full bg-brand-pa-600" />}
                      {phase === 'done' && <span className="text-signal-ok">✓</span>}
                    </div>
                    <div className="mt-1 text-sm font-semibold text-ink-900">{s.label}</div>
                    {detail && <div className="mt-0.5 text-[11px] text-ink-500">{detail}</div>}
                  </li>
                );
              })}
            </ol>
            {run.error && (
              <div className="mt-3 rounded-md border border-signal-err bg-red-50 px-3 py-2 text-xs text-signal-err">
                에러: {run.error}
              </div>
            )}
            <div className="mt-3 flex items-center justify-between text-[11px] text-ink-400">
              <span>run #{run.id} · 시작 {run.started_at}</span>
              {run.finished_at && <span>완료 {run.finished_at}</span>}
            </div>
          </>
        )}
      </Card>

      <Card
        title={`키워드 (${keywordsQ.data?.total || 0})`}
        action={
          <div className="flex items-center gap-3">
            <span className="text-xs text-ink-500">
              미분류: <b>{keywordsQ.data?.unclustered ?? '—'}</b>개
            </span>
            <Button size="sm" variant="secondary" onClick={() => clusterMut.mutate()} disabled={clusterMut.isPending}>
              클러스터링 실행
            </Button>
          </div>
        }
        padded={false}
      >
        {keywordsQ.isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable columns={COLS} rows={keywordsQ.data?.items || []} rowKey={(r) => r.id} />
        )}
      </Card>

      <Card title="클러스터">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {(clustersQ.data || []).map((c) => (
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
