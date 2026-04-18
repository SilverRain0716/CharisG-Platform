import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge, Button, KPICard } from '@charisg/ui';
import { ds } from '../api/ds.js';

/* ── Tab: 전체 후보 ─────────────────────────────── */

const ALL_COLS = [
  { key: 'id', label: 'ID', sortable: true, width: '60px' },
  { key: 'product_name', label: '상품명', maxWidth: '300px', wrap: true },
  { key: 'amazon_category', label: '카테고리', width: '140px' },
  { key: 'source_price', label: 'CJ', sortable: true, width: '70px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '\u2014' },
  { key: 'real_margin_pct', label: '실질%', sortable: true, width: '70px',
    render: (v) => v != null ? Number(v).toFixed(1) + '%' : '\u2014' },
  { key: 'matrix_group', label: 'Mat', width: '50px' },
  { key: 'sort_score', label: 'Sort', sortable: true, width: '70px',
    render: (v) => v != null ? Number(v).toFixed(3) : '\u2014' },
  { key: 'go_decision', label: 'GO', width: '90px',
    render: (v) => v ? <StatusBadge variant={v === 'GO' ? 'ok' : v === 'GO_ORGANIC' ? 'info' : 'err'}>{v}</StatusBadge> : '\u2014' },
  { key: 'matched_asin', label: 'ASIN', width: '130px',
    render: (v) => v ? <span className="font-mono text-xs">{v}</span> : <StatusBadge variant="neutral">미매칭</StatusBadge> },
  { key: 'status', label: '상태', width: '90px' },
];

function TabAll() {
  const [filterGo, setFilterGo] = useState('');
  const { data, isLoading } = useQuery({
    queryKey: ['ds', 'products', filterGo],
    queryFn: () => ds.products(filterGo ? { go: filterGo } : {}),
  });

  return (
    <>
      <div className="flex items-center justify-between mb-4">
        <p className="text-sm text-ink-500">{data?.total || 0}건 \u2014 sort_score 내림차순</p>
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
      </div>
      <Card padded={false}>
        {isLoading ? (
          <div className="p-10 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={ALL_COLS}
            rows={data?.items || []}
            rowKey={(r) => r.id}
            defaultSort={{ key: 'sort_score', dir: 'desc' }}
          />
        )}
      </Card>
    </>
  );
}

/* ── Tab: ASIN 매칭 ─────────────────────────────── */

const MATCH_COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'product_name', label: '상품명' },
  { key: 'source_price', label: 'CJ', width: '70px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '\u2014' },
  { key: 'sort_score', label: 'Sort', width: '70px',
    render: (v) => v != null ? Number(v).toFixed(3) : '\u2014' },
  { key: 'matched_asin', label: 'ASIN 매칭', width: '130px',
    render: (v) => v ? <span className="font-mono text-xs">{v}</span> : <StatusBadge variant="neutral">미매칭</StatusBadge> },
];

function TabMatch() {
  const qc = useQueryClient();
  const [expandedId, setExpandedId] = useState(null);

  const summary = useQuery({ queryKey: ['ds', 'asin-summary'], queryFn: ds.asinSummary });
  const products = useQuery({
    queryKey: ['ds', 'products', 'all'],
    queryFn: () => ds.products({ limit: 500 }),
  });
  const progress = useQuery({
    queryKey: ['ds', 'match-progress'],
    queryFn: ds.matchProgress,
    refetchInterval: (q) => q.state.data?.running ? 1500 : false,
  });
  const candidates = useQuery({
    queryKey: ['ds', 'candidates', expandedId],
    queryFn: () => ds.matchCandidates(expandedId),
    enabled: !!expandedId,
  });

  const runBatch = useMutation({
    mutationFn: () => ds.matchBatch({ limit: 50 }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'match-progress'] }),
  });
  const selectAsin = useMutation({
    mutationFn: ({ id, asin }) => ds.matchSelect(id, asin),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ds', 'products'] });
      qc.invalidateQueries({ queryKey: ['ds', 'asin-summary'] });
      qc.invalidateQueries({ queryKey: ['ds', 'candidates', expandedId] });
    },
  });

  const s = summary.data || {};
  const prog = progress.data || {};
  const running = prog.running;
  const done = prog.phase === 'done';

  // GO 상품만 필터
  const goItems = (products.data?.items || []).filter((p) => p.go_decision === 'GO' || p.go_decision === 'GO_ORGANIC');

  return (
    <>
      <div className="grid grid-cols-3 gap-4 mb-4">
        <KPICard label="미매칭" value={s.unmatched ?? '\u2014'} accent="ds" />
        <KPICard label="매칭 완료" value={s.matched ?? '\u2014'} accent="ds" />
        <KPICard label="필터 통과" value={s.total_filtered ?? '\u2014'} accent="ds" />
      </div>

      <div className="flex items-center gap-3 mb-4">
        <Button variant="ds" onClick={() => runBatch.mutate()} disabled={runBatch.isPending || running}>
          {running ? '매칭 중...' : '일괄 ASIN 매칭'}
        </Button>
        {(running || done) && (
          <div className="flex-1">
            <div className="flex items-center justify-between text-xs text-ink-500 mb-1">
              <span>{prog.message || ''}</span>
              <span>{prog.current}/{prog.total}</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100">
              <div
                className="h-full bg-brand-ds-500 transition-all"
                style={{ width: prog.total ? `${(prog.current / prog.total) * 100}%` : '0%' }}
              />
            </div>
            {done && prog.result && (
              <p className="mt-1 text-xs text-ink-500">
                처리 {prog.result.processed} / 매칭 {prog.result.matched} / 실패 {prog.result.failed}
              </p>
            )}
          </div>
        )}
      </div>

      <Card padded={false}>
        <table className="w-full text-sm">
          <thead className="bg-ink-50 text-left text-xs font-medium text-ink-500">
            <tr>
              <th className="px-3 py-2 w-[60px]">ID</th>
              <th className="px-3 py-2">상품명</th>
              <th className="px-3 py-2 w-[70px]">CJ</th>
              <th className="px-3 py-2 w-[70px]">Sort</th>
              <th className="px-3 py-2 w-[130px]">ASIN 매칭</th>
              <th className="px-3 py-2 w-[60px]"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-100">
            {goItems.map((p) => (
              <React.Fragment key={p.id}>
                <tr className="hover:bg-ink-50">
                  <td className="px-3 py-2 text-ink-500">{p.id}</td>
                  <td className="px-3 py-2 text-ink-900 max-w-[300px] truncate">{p.product_name}</td>
                  <td className="px-3 py-2">${Number(p.source_price || 0).toFixed(2)}</td>
                  <td className="px-3 py-2">{Number(p.sort_score || 0).toFixed(3)}</td>
                  <td className="px-3 py-2">
                    {p.matched_asin
                      ? <span className="font-mono text-xs">{p.matched_asin}</span>
                      : <StatusBadge variant="neutral">미매칭</StatusBadge>}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => setExpandedId(expandedId === p.id ? null : p.id)}
                      className="text-xs text-brand-ds-600 hover:underline"
                    >
                      {expandedId === p.id ? '접기' : '후보'}
                    </button>
                  </td>
                </tr>
                {expandedId === p.id && (
                  <tr>
                    <td colSpan={6} className="bg-ink-50 px-6 py-3">
                      {candidates.isLoading ? (
                        <p className="text-xs text-ink-400">후보 로딩...</p>
                      ) : (candidates.data?.candidates || []).length === 0 ? (
                        <p className="text-xs text-ink-400">후보 없음 (매칭 먼저 실행)</p>
                      ) : (
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="text-ink-500">
                              <th className="text-left pb-1">ASIN</th>
                              <th className="text-left pb-1">Amazon 제목</th>
                              <th className="text-right pb-1">점수</th>
                              <th className="text-left pb-1">판정</th>
                              <th className="pb-1"></th>
                            </tr>
                          </thead>
                          <tbody>
                            {(candidates.data?.candidates || []).map((c) => (
                              <tr key={c.asin} className={c.selected ? 'bg-brand-ds-50' : ''}>
                                <td className="py-1 font-mono">{c.asin}</td>
                                <td className="py-1 max-w-[250px] truncate">{c.amazon_title}</td>
                                <td className="py-1 text-right">{Number(c.match_score).toFixed(3)}</td>
                                <td className="py-1">
                                  <StatusBadge variant={c.match_verdict === 'strong' ? 'ok' : c.match_verdict === 'moderate' ? 'info' : 'warn'}>
                                    {c.match_verdict}
                                  </StatusBadge>
                                </td>
                                <td className="py-1 text-right">
                                  {!c.selected && (
                                    <button
                                      onClick={() => selectAsin.mutate({ id: p.id, asin: c.asin })}
                                      className="text-brand-ds-600 hover:underline"
                                    >
                                      선택
                                    </button>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

/* ── Tab: 업로드 대기 ───────────────────────────── */

function TabUpload() {
  const qc = useQueryClient();

  const summary = useQuery({ queryKey: ['ds', 'asin-summary'], queryFn: ds.asinSummary });
  const products = useQuery({
    queryKey: ['ds', 'products', 'matched'],
    queryFn: () => ds.products({ limit: 500 }),
  });
  const progress = useQuery({
    queryKey: ['ds', 'offer-progress'],
    queryFn: ds.offerProgress,
    refetchInterval: (q) => q.state.data?.running ? 1500 : false,
  });

  const runBatch = useMutation({
    mutationFn: (dryRun) => ds.offerBatch({ limit: 20, dry_run: dryRun }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'offer-progress'] }),
  });

  const s = summary.data || {};
  const prog = progress.data || {};
  const running = prog.running;
  const done = prog.phase === 'done';

  // 매칭 완료된 상품만
  const matched = (products.data?.items || []).filter((p) => p.matched_asin);

  return (
    <>
      <div className="grid grid-cols-3 gap-4 mb-4">
        <KPICard label="매칭 완료" value={s.matched ?? '\u2014'} accent="ds" />
        <KPICard label="등록 완료" value={s.listed ?? '\u2014'} accent="ds" />
        <KPICard label="활성" value={s.active ?? '\u2014'} accent="ds" />
      </div>

      <div className="flex items-center gap-3 mb-4">
        <Button variant="ds" onClick={() => runBatch.mutate(true)} disabled={runBatch.isPending || running}>
          Offer 검증
        </Button>
        <Button variant="secondary" onClick={() => runBatch.mutate(false)} disabled={runBatch.isPending || running}>
          Push to Amazon
        </Button>
        {(running || done) && (
          <div className="flex-1">
            <div className="flex items-center justify-between text-xs text-ink-500 mb-1">
              <span>{prog.message || ''}</span>
              <span>{prog.current}/{prog.total}</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100">
              <div
                className="h-full bg-brand-ds-500 transition-all"
                style={{ width: prog.total ? `${(prog.current / prog.total) * 100}%` : '0%' }}
              />
            </div>
            {done && prog.result && (
              <p className="mt-1 text-xs text-ink-500">
                처리 {prog.result.processed} / 성공 {prog.result.success} / 실패 {prog.result.failed}
                {prog.result.dry_run && ' (검증 모드)'}
              </p>
            )}
          </div>
        )}
      </div>

      <Card padded={false}>
        <DataTable
          columns={[
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'product_name', label: '상품명' },
            { key: 'matched_asin', label: 'ASIN', width: '130px',
              render: (v) => <span className="font-mono text-xs">{v}</span> },
            { key: 'calculated_price', label: '판매가', width: '80px',
              render: (v) => v != null ? '$' + Number(v).toFixed(2) : '\u2014' },
            { key: 'real_margin_pct', label: '마진%', width: '70px',
              render: (v) => v != null ? Number(v).toFixed(1) + '%' : '\u2014' },
            { key: 'status', label: '상태', width: '90px',
              render: (v) => (
                <StatusBadge variant={v === 'listed' ? 'ok' : v === 'active' ? 'ok' : 'neutral'}>
                  {v}
                </StatusBadge>
              ) },
          ]}
          rows={matched}
          rowKey={(r) => r.id}
          defaultSort={{ key: 'sort_score', dir: 'desc' }}
          emptyText="매칭된 상품이 없습니다. ASIN 매칭 탭에서 먼저 매칭하세요."
        />
      </Card>
    </>
  );
}

/* ── Main: 탭 컨테이너 ──────────────────────────── */

const TABS = [
  { id: 'all', label: '전체 후보' },
  { id: 'match', label: 'ASIN 매칭' },
  { id: 'upload', label: '업로드 대기' },
];

export default function ProductList() {
  const [tab, setTab] = useState('all');

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">상품 후보</h1>
        <p className="mt-1 text-sm text-ink-500">CJ 소싱 상품 관리 \u2014 매칭 \u2014 Amazon 업로드.</p>
      </header>

      <div className="flex gap-1 border-b border-ink-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={
              'px-4 py-2 text-sm font-medium border-b-2 transition-colors ' +
              (tab === t.id
                ? 'border-brand-ds-500 text-brand-ds-700'
                : 'border-transparent text-ink-500 hover:text-ink-900')
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'all' && <TabAll />}
      {tab === 'match' && <TabMatch />}
      {tab === 'upload' && <TabUpload />}
    </div>
  );
}
