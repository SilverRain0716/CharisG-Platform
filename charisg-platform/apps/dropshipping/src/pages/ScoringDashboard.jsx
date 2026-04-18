import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import { Card, Button } from '@charisg/ui';
import { useMarket } from '../App.jsx';

const PHASE_LABEL = {
  idle: '대기',
  collect: 'CJ 카탈로그 수집 중',
  score: '스코어링 중',
  done: '완료',
  error: '오류',
};

const CELL_COLOR = {
  AA: 'bg-emerald-500', AB: 'bg-emerald-400',
  BA: 'bg-sky-500', BB: 'bg-sky-400',
  AC: 'bg-amber-400', BC: 'bg-amber-500', CA: 'bg-amber-500',
  CB: 'bg-red-400', CC: 'bg-red-500',
};

export default function ScoringDashboard() {
  const { market, ds } = useMarket();
  const qc = useQueryClient();
  const matrix = useQuery({ queryKey: ['ds', 'matrix', market], queryFn: ds.scoringMatrix });
  const dist = useQuery({ queryKey: ['ds', 'dist', market], queryFn: ds.scoringDist });
  const fails = useQuery({ queryKey: ['ds', 'fails', market], queryFn: ds.filterFails });
  const progress = useQuery({
    queryKey: ['ds', 'progress', market],
    queryFn: ds.scoringProgress,
    refetchInterval: (q) => (q.state.data?.running ? 1500 : false),
  });
  const run = useMutation({
    mutationFn: () => ds.runScoring({ collect_cj: 'true', use_trends: 'true' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ds', 'progress', market] });
    },
  });

  // 완료 시 데이터 리프레시
  React.useEffect(() => {
    if (progress.data?.phase === 'done') {
      qc.invalidateQueries({ queryKey: ['ds', 'matrix'] });
      qc.invalidateQueries({ queryKey: ['ds', 'dist'] });
      qc.invalidateQueries({ queryKey: ['ds', 'fails'] });
    }
  }, [progress.data?.phase, qc]);

  const state = progress.data || { running: false, phase: 'idle', current: 0, total: 0, message: '' };
  const pct = state.total > 0 ? Math.min(100, Math.round((state.current / state.total) * 100)) : 0;

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">스코어링</h1>
          <p className="mt-1 text-sm text-ink-500">
            CJ 카탈로그 수집 → Hard Filter → Demand × Gap × Margin 3축 스코어링.
          </p>
        </div>
        <Button
          variant="ds"
          onClick={() => run.mutate()}
          disabled={run.isPending || state.running}
        >
          {state.running ? PHASE_LABEL[state.phase] || '실행 중...' : '스코어링 실행'}
        </Button>
      </header>

      {/* 파이프라인 진행 상태 */}
      {(state.running || state.phase === 'done' || state.phase === 'error') && (
        <Card title="파이프라인 진행 상태">
          <div className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium text-ink-700">
                {PHASE_LABEL[state.phase] || state.phase}
              </span>
              <span className="text-ink-500">
                {state.total > 0 ? `${state.current} / ${state.total}` : ''}
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100">
              <div
                className={`h-full transition-all ${
                  state.phase === 'error' ? 'bg-red-500' : 'bg-brand-ds-500'
                }`}
                style={{ width: `${state.phase === 'done' ? 100 : pct}%` }}
              />
            </div>
            {state.message && (
              <div className="text-xs text-ink-500">{state.message}</div>
            )}
            {state.error && (
              <div className="rounded-md bg-red-50 p-2 text-xs text-red-700">
                {state.error}
              </div>
            )}
          </div>
        </Card>
      )}

      <Card title="3×3 매트릭스 (Demand × Margin)">
        <div className="overflow-x-auto">
          <table className="mx-auto border-separate border-spacing-1">
            <thead>
              <tr>
                <th></th>
                {['A', 'B', 'C'].map((m) => (
                  <th key={m} className="px-3 py-1 text-xs font-semibold text-ink-500">Margin {m}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {['A', 'B', 'C'].map((d) => (
                <tr key={d}>
                  <th className="px-3 py-1 text-right text-xs font-semibold text-ink-500">Demand {d}</th>
                  {['A', 'B', 'C'].map((m) => {
                    const cell = matrix.data?.cells?.find((c) => c.demand === d && c.margin === m);
                    return (
                      <td key={m}>
                        <div className={`flex h-24 w-32 flex-col items-center justify-center rounded-md text-white ${CELL_COLOR[d + m] || 'bg-ink-300'}`}>
                          <div className="text-xl font-bold">{cell?.count || 0}</div>
                          <div className="text-xs opacity-90">GO {cell?.go_ratio || 0}%</div>
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Hard Filter 탈락 사유">
          <div className="h-64">
            <ResponsiveContainer>
              <BarChart data={fails.data || []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="reason" tick={{ fontSize: 11 }} />
                <YAxis />
                <Tooltip />
                <Bar dataKey="count" fill="#0d9488" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card title="스코어 분포 (Demand)">
          <div className="h-64">
            <ResponsiveContainer>
              <BarChart data={dist.data?.demand || []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="bin" tick={{ fontSize: 11 }} />
                <YAxis />
                <Tooltip />
                <Bar dataKey="count" fill="#14b8a6" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>
    </div>
  );
}
