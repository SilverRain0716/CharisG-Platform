import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const REFRESH_MS = 5_000;      // 일반 섹션
const LIVE_REFRESH_MS = 3_000; // 라이브 진행률

/** 카운트 + label 카드 */
function StatBox({ label, value, tone = 'default', sub = null }) {
  const toneCls =
    tone === 'ok'    ? 'border-signal-ok/40 bg-soft-ok text-signal-ok' :
    tone === 'warn'  ? 'border-signal-warn/40 bg-soft-warn text-signal-warn'       :
    tone === 'error' ? 'border-signal-err/40 bg-soft-err text-signal-err'             :
    'border-ink-200 bg-surface text-ink-900';
  return (
    <div className={`rounded-lg border p-3 ${toneCls}`}>
      <div className="text-xs">{label}</div>
      <div className="mt-1 text-xl font-semibold">{(value ?? 0).toLocaleString()}</div>
      {sub && <div className="mt-1 text-xs opacity-70">{sub}</div>}
    </div>
  );
}

/** 단계별 카드 — 진척도 막대 + done/in_progress/error */
function StageCard({ stage }) {
  const total = stage.total || 0;
  const donePct = total > 0 ? Math.round((stage.done / total) * 100) : 0;
  return (
    <Card title={stage.label}>
      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <span className="text-2xl font-semibold">{stage.done.toLocaleString()}</span>
          <span className="text-xs text-ink-500">/ 전체 {total.toLocaleString()}</span>
        </div>
        <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100">
          <div className="h-full bg-signal-ok" style={{ width: `${donePct}%` }} />
        </div>
        <div className="flex gap-3 text-xs">
          <span className="text-signal-ok">✓ {stage.done.toLocaleString()}</span>
          <span className="text-signal-warn">⏳ {stage.in_progress.toLocaleString()}</span>
          <span className="text-signal-err">⛔ {stage.error.toLocaleString()}</span>
        </div>
        {stage.detail && (
          <div className="text-xs text-ink-500">
            {Object.entries(stage.detail).map(([k, v]) => `${k}:${v}`).join(' · ')}
          </div>
        )}
      </div>
    </Card>
  );
}

// 시트 임포트 단계별 진척도 (sheet_queue) — 2026-06-02
function SheetImportCard({ jobs, batch }) {
  const active = jobs.find((j) => !['done', 'error', 'cancelled'].includes(j.status));
  const job = active || jobs[0];
  if (!job) return null;
  const running = !['done', 'error', 'cancelled'].includes(job.status);
  const total = job.imported || 0;
  const hasCp = (job.target_channels || '').includes('coupang');
  const hasSs = (job.target_channels || '').includes('smartstore');
  // 현재 실행 중인 배치(AI 상세) 실시간 진행 — /detail-page/batch (job_type='ai_detail')
  const liveDone = batch && batch.total ? (batch.processed + (batch.errors || 0)) : null;
  const st = job.status; // importing/promoting/detailing/channelsending/done
  // 각 단계: done=완료수, live=현재단계 실시간 진행(있으면), of=분모
  const stages = [
    { key: '1. 임포트', done: total, of: total, live: st === 'importing' ? null : total },
    { key: '2. Promote(게이트)', done: job.promoted || 0, of: total, live: st === 'promoting' ? liveDone : (job.promoted || 0) },
    { key: '3. AI 상세', done: job.detailed || 0, of: total, live: st === 'detailing' ? liveDone : (job.detailed || 0) },
    ...(hasCp ? [{ key: '4. 쿠팡 리스팅', done: job.coupang_listed || 0, of: total, live: job.coupang_listed || 0, fail: job.coupang_failed || 0 }] : []),
    ...(hasSs ? [{ key: '5. 네이버 리스팅', done: job.smartstore_listed || 0, of: total, live: job.smartstore_listed || 0, fail: job.smartstore_failed || 0 }] : []),
  ];
  const tone = running ? 'border-signal-ok/40 bg-soft-ok'
    : st === 'error' ? 'border-signal-err/40 bg-soft-err' : 'border-ink-200 bg-ink-50';
  const stageActive = { importing: 0, promoting: 1, detailing: 2, channelsending: 3 }[st];
  return (
    <Card padded={false}>
      <div className={`flex items-center justify-between rounded-t-xl border-b px-4 py-3 ${tone}`}>
        <span className="text-sm font-semibold text-ink-800">
          {running ? '🟢 시트 임포트 진행 중' : st === 'error' ? '🔴 임포트 에러' : '⚪ 최근 임포트'}
          {job.sheet_label ? ` — ${job.sheet_label}` : ''}
        </span>
        <span className="text-xs text-ink-500">{job.current_step || st}</span>
      </div>
      <div className="space-y-2.5 p-4">
        {stages.map((s, i) => {
          const isCur = stageActive === i;
          const v = isCur && s.live != null ? s.live : s.done;
          const pct = s.of ? Math.min(100, Math.round((v / s.of) * 1000) / 10) : 0;
          return (
            <div key={s.key}>
              <div className="mb-0.5 flex justify-between text-xs">
                <span className={isCur ? 'font-semibold text-signal-ok' : 'text-ink-600'}>
                  {s.key}{isCur ? ' ⟳' : ''}
                </span>
                <span className="text-ink-500">
                  {v.toLocaleString()} / {s.of.toLocaleString()} ({pct}%)
                  {s.fail ? <span className="text-signal-err"> · 실패 {s.fail}</span> : null}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-ink-100">
                <div className={`h-full rounded-full transition-all duration-500 ${isCur ? 'bg-signal-ok' : 'bg-ink-400'}`}
                  style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function LiveProgressCard({ live }) {
  if (!live) return null;
  const tone = live.worker_active ? 'border-signal-ok/40 bg-soft-ok' : 'border-ink-200 bg-ink-50';
  const remaining = live.queue_remaining ?? 0;
  const done = live.queue_done ?? 0;
  const total = live.queue_total ?? 0;
  const pct = live.progress_pct ?? 0;
  return (
    <div className={`rounded-xl border p-4 ${tone}`}>
      <div className="flex flex-wrap items-baseline gap-3">
        <span className={`text-sm font-medium ${live.worker_active ? 'text-signal-ok' : 'text-ink-500'}`}>
          {live.worker_active ? '🟢 워커 가동 중' : '⚪ 워커 대기 (다음 22:00 UTC)'}
        </span>
        {live.in_flight && (
          <span className="text-sm text-ink-700">
            처리 중: <b>{live.in_flight.parent_asin}</b>
            <span className="ml-1 text-xs text-ink-500">({live.in_flight.status}, {live.in_flight.duration_sec}s)</span>
          </span>
        )}
      </div>
      <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-ink-100">
        <div className="h-full bg-signal-ok transition-all" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 flex flex-wrap gap-4 text-xs text-ink-600">
        <span><b className="text-ink-900">{pct}%</b> ({done.toLocaleString()} / {total.toLocaleString()})</span>
        <span>잔여 <b>{remaining.toLocaleString()}</b></span>
        <span>분당 <b>{live.rate?.per_minute ?? 0}</b>건 · 시간당 <b>{live.rate?.per_hour ?? 0}</b></span>
        <span>최근 5분: ✓ {live.rate?.last_5min_done ?? 0} · ⛔ {live.rate?.last_5min_skipped ?? 0}</span>
        {live.eta_human && <span>ETA <b>{live.eta_human}</b></span>}
        {live.next_trigger && (
          <span>다음 가동 <b>{new Date(live.next_trigger).toLocaleString('ko-KR', { timeZone: 'UTC' })} UTC</b></span>
        )}
      </div>
    </div>
  );
}

export default function AutomationPage() {
  const [stageFilter, setStageFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const live = useQuery({
    queryKey: ['pa', 'automation', 'live'],
    queryFn: pa.automationLive,
    refetchInterval: LIVE_REFRESH_MS,
  });

  const pipeline = useQuery({
    queryKey: ['pa', 'automation', 'pipeline'],
    queryFn: pa.automationPipeline,
    refetchInterval: REFRESH_MS,
  });
  const gates = useQuery({
    queryKey: ['pa', 'automation', 'gates'],
    queryFn: pa.automationGates,
    refetchInterval: REFRESH_MS,
  });
  const workers = useQuery({
    queryKey: ['pa', 'automation', 'workers'],
    queryFn: pa.automationWorkers,
    refetchInterval: REFRESH_MS,
  });
  const errors = useQuery({
    queryKey: ['pa', 'automation', 'errors', stageFilter, offset],
    queryFn: () => pa.automationErrors({
      ...(stageFilter ? { stage: stageFilter } : {}),
      limit, offset,
    }),
    refetchInterval: REFRESH_MS,
  });
  const sheetImport = useQuery({
    queryKey: ['pa', 'sourcing', 'queue'],
    queryFn: pa.sheetQueue,
    refetchInterval: LIVE_REFRESH_MS,
  });
  const curBatch = useQuery({
    queryKey: ['pa', 'detail', 'batch-current'],
    queryFn: pa.getCurrentBatchJob,
    refetchInterval: LIVE_REFRESH_MS,
  });

  const stages = pipeline.data?.stages || [];
  const gateRows = gates.data?.gates || [];
  const workerRows = workers.data?.workers || [];
  const errorRows = errors.data?.errors || [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">자동화</h1>
        <p className="mt-1 text-sm text-ink-500">
          파이프라인 진척도, 정책 게이트 차단, 워커/Timer 상태, 에러 로그 — 라이브 3초 / 일반 5초 자동 갱신.
        </p>
      </header>

      {/* 0. 시트 임포트 단계별 진척도 (라이브) */}
      <SheetImportCard jobs={sheetImport.data?.items || []} batch={curBatch.data?.job} />

      {/* 0b. 그룹 워커 라이브 */}
      <LiveProgressCard live={live.data} />

      {/* 1. 파이프라인 진척도 */}
      <section>
        <h2 className="mb-3 text-sm font-semibold text-ink-700">1. 파이프라인 진척도</h2>
        {pipeline.isLoading ? (
          <div className="text-sm text-ink-400">로딩 중…</div>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            {stages.map((s) => <StageCard key={s.key} stage={s} />)}
          </div>
        )}
      </section>

      {/* 2. 게이트별 차단 통계 */}
      <section>
        <h2 className="mb-3 text-sm font-semibold text-ink-700">2. 정책 게이트 차단 통계</h2>
        {gates.isLoading ? (
          <div className="text-sm text-ink-400">로딩 중…</div>
        ) : (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
            {gateRows.map((g) => (
              <div key={g.key} className="rounded-md border border-ink-200 bg-surface p-3">
                <div className="text-xs text-ink-500">{g.label}</div>
                <div className="mt-1 flex items-baseline gap-2">
                  <span className="text-lg font-semibold">{g.total.toLocaleString()}</span>
                  <span className="text-xs text-ink-400">총</span>
                </div>
                <div className="mt-1 flex gap-2 text-xs">
                  <span className="text-ink-500">24h: <b className="text-ink-800">{g.h24}</b></span>
                  <span className="text-ink-500">7d: <b className="text-ink-800">{g.d7}</b></span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 3. 워커 / Timer 상태 */}
      <section>
        <h2 className="mb-3 text-sm font-semibold text-ink-700">3. 워커 / Timer 상태</h2>
        <Card padded={false}>
          <DataTable
            columns={[
              { key: 'label', label: '잡' },
              { key: 'unit', label: 'unit', width: '320px' },
              { key: 'active', label: '상태', width: '90px',
                render: (v) => v
                  ? <StatusBadge variant="ok">active</StatusBadge>
                  : <StatusBadge variant="muted">inactive</StatusBadge> },
              { key: 'sub_state', label: 'sub', width: '110px' },
              { key: 'memory_mb', label: 'mem(MB)', width: '90px',
                render: (v) => v != null ? v.toLocaleString() : '—' },
              { key: 'started_at', label: '시작', width: '200px' },
            ]}
            rows={workerRows}
            rowKey={(r) => r.unit}
          />
        </Card>
      </section>

      {/* 4. 에러 로그 */}
      <section>
        <h2 className="mb-3 text-sm font-semibold text-ink-700">4. 에러 로그</h2>
        <div className="mb-2 flex flex-wrap gap-2">
          {['', 'uploading', 'group_queue', 'sheet', 'batch'].map((s) => (
            <button
              key={s || 'all'}
              type="button"
              onClick={() => { setStageFilter(s); setOffset(0); }}
              className={`rounded-md border px-3 py-1 text-xs ${
                stageFilter === s
                  ? 'border-accent bg-accent/10 text-accent'
                  : 'border-ink-200 bg-surface text-ink-700 hover:bg-ink-50'
              }`}
            >
              {s || '전체'}
            </button>
          ))}
          <div className="ml-auto flex gap-1">
            <button
              type="button"
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={offset === 0}
              className="rounded-md border border-ink-200 px-2 py-1 text-xs disabled:opacity-30"
            >이전</button>
            <span className="text-xs text-ink-500 self-center px-1">
              {offset + 1}–{offset + errorRows.length}
            </span>
            <button
              type="button"
              onClick={() => setOffset(offset + limit)}
              disabled={errorRows.length < limit}
              className="rounded-md border border-ink-200 px-2 py-1 text-xs disabled:opacity-30"
            >다음</button>
          </div>
        </div>
        <Card padded={false}>
          <DataTable
            columns={[
              { key: 'ts', label: '시각', width: '180px' },
              { key: 'stage', label: '단계', width: '120px',
                render: (v) => <StatusBadge variant="muted">{v}</StatusBadge> },
              { key: 'asin', label: 'ASIN/식별', width: '160px' },
              { key: 'msg', label: '사유' },
            ]}
            rows={errorRows}
            rowKey={(r, i) => `${r.stage}-${r.asin}-${r.ts}-${i}`}
          />
        </Card>
      </section>
    </div>
  );
}
