import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

function InlinePrice({ row, onSave }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState('');
  const v = row.sale_price_krw;

  if (!editing) {
    return (
      <span
        className="cursor-pointer hover:text-indigo-600 hover:underline"
        title="클릭하여 판매가 수정"
        onClick={() => { setVal(v || ''); setEditing(true); }}
      >
        {v != null ? '₩' + Number(v).toLocaleString() : '—'}
      </span>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        step="100"
        value={val}
        onChange={(e) => setVal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { onSave(row.id, Number(val)); setEditing(false); }
          if (e.key === 'Escape') setEditing(false);
        }}
        autoFocus
        className="w-24 rounded border border-ink-300 px-1.5 py-0.5 text-sm outline-none focus:border-indigo-500"
      />
      <button onClick={() => { onSave(row.id, Number(val)); setEditing(false); }} className="text-green-600 text-xs font-bold">✓</button>
      <button onClick={() => setEditing(false)} className="text-ink-400 text-xs">✕</button>
    </div>
  );
}

const COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'title_ko', label: '상품명', wrap: true, maxWidth: '360px',
    render: (v, row) => v || <span className="text-ink-400">{row.title_en || '—'}</span> },
  { key: 'seo_title', label: 'SEO 제목', wrap: true, maxWidth: '200px',
    render: (v) => v || <span className="text-ink-300">—</span> },
  { key: 'margin_pct', label: '마진%', sortable: true, width: '80px',
    render: (v) => v != null ? Number(v).toFixed(1) + '%' : '—' },
  { key: 'ai_processed_at', label: 'AI', width: '60px',
    render: (v) => v ? <span className="text-green-600 font-medium">✓</span>
                     : <span className="text-ink-300">—</span> },
  { key: 'status', label: '상태', width: '90px',
    render: (v) => <StatusBadge variant={v === 'active' ? 'ok' : v === 'paused' ? 'warn' : 'neutral'}>{v}</StatusBadge> },
];

export default function ProductManagementPage() {
  const qc = useQueryClient();
  const [showAll, setShowAll] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'products', showAll ? 'all' : 'unchanneled'],
    queryFn: () => pa.products({ limit: 200, ...(showAll ? {} : { unchanneled_only: 'true' }) }),
  });

  const [batchProgress, setBatchProgress] = useState(null);
  const [naverProgress, setNaverProgress] = useState(null);
  const [coupangProgress, setCoupangProgress] = useState(null);
  const [generatingId, setGeneratingId] = useState(null);
  const [previewHtml, setPreviewHtml] = useState(null);
  const jobIdRef = useRef(null);
  const naverJobIdRef = useRef(null);
  const coupangJobIdRef = useRef(null);
  const pollRef = useRef(null);

  const _jobToProgress = (job) => ({
    pct: job.pct ?? 0,
    current: (job.processed || 0) + (job.errors || 0),
    total: job.total || 0,
    processed: job.processed || 0,
    errors: job.errors || 0,
    status: job.status,
    phase: job.phase_message,
    message: job.error_message,
  });

  // 폴링: 3개 job 모두 2초마다 상태 조회
  useEffect(() => {
    const poll = async () => {
      if (jobIdRef.current) {
        try {
          const job = await pa.getBatchJobStatus(jobIdRef.current);
          const done = job.status === 'done' || job.status === 'error';
          setBatchProgress(_jobToProgress(job));
          if (done) {
            jobIdRef.current = null;
            qc.invalidateQueries({ queryKey: ['pa', 'products'] });
          }
        } catch {}
      }
      if (naverJobIdRef.current) {
        try {
          const job = await pa.naverCategoryJobStatus(naverJobIdRef.current);
          const done = job.status === 'done' || job.status === 'error';
          setNaverProgress(_jobToProgress(job));
          if (done) {
            naverJobIdRef.current = null;
            qc.invalidateQueries({ queryKey: ['pa', 'products'] });
          }
        } catch {}
      }
      if (coupangJobIdRef.current) {
        try {
          const job = await pa.coupangCategoryJobStatus(coupangJobIdRef.current);
          const done = job.status === 'done' || job.status === 'error';
          setCoupangProgress(_jobToProgress(job));
          if (done) {
            coupangJobIdRef.current = null;
            qc.invalidateQueries({ queryKey: ['pa', 'products'] });
          }
        } catch {}
      }
    };
    pollRef.current = setInterval(poll, 2000);
    return () => clearInterval(pollRef.current);
  }, [qc]);

  // 페이지 진입 시 실행 중인 job 자동 감지 (3종)
  useEffect(() => {
    (async () => {
      try {
        const res = await pa.getCurrentBatchJob();
        if (res.job) {
          jobIdRef.current = res.job.id;
          setBatchProgress(_jobToProgress(res.job));
        }
      } catch {}
      try {
        const res = await pa.currentNaverCategoryJob();
        if (res.job) {
          naverJobIdRef.current = res.job.id;
          setNaverProgress(_jobToProgress(res.job));
        }
      } catch {}
      try {
        const res = await pa.currentCoupangCategoryJob();
        if (res.job) {
          coupangJobIdRef.current = res.job.id;
          setCoupangProgress(_jobToProgress(res.job));
        }
      } catch {}
    })();
  }, []);

  const handlePreview = async (productId) => {
    try {
      const detail = await pa.getDetailPage(productId);
      setPreviewHtml(detail.html_content || '<p>상세페이지 없음</p>');
    } catch {
      setPreviewHtml('<p>상세페이지를 불러올 수 없습니다.</p>');
    }
  };

  const generate = useMutation({
    mutationFn: (pid) => pa.generateDetail(pid),
    onMutate: (pid) => setGeneratingId(pid),
    onSettled: () => {
      setGeneratingId(null);
      qc.invalidateQueries({ queryKey: ['pa', 'products'] });
    },
  });

  const updatePrice = useMutation({
    mutationFn: ({ pid, price }) => pa.updateProductPrice(pid, price),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pa', 'products'] }),
  });
  const handlePriceSave = (pid, price) => {
    if (price > 0) updatePrice.mutate({ pid, price });
  };

  const [sendingId, setSendingId] = useState(null);
  const sendChannel = useMutation({
    mutationFn: (pid) => pa.sendToChannel(pid),
    onMutate: (pid) => setSendingId(pid),
    onSettled: () => {
      setSendingId(null);
      qc.invalidateQueries({ queryKey: ['pa', 'products'] });
    },
  });

  const startBatchJob = useCallback(async (body) => {
    setBatchProgress({ pct: 0, current: 0, total: 0, status: 'running' });
    try {
      const res = await pa.startBatchJob(body);
      jobIdRef.current = res.job_id;
    } catch (e) {
      setBatchProgress({ pct: 0, status: 'error', message: e.message || '배치 시작 실패' });
    }
  }, []);

  const startNaverMapJob = useCallback(async () => {
    setNaverProgress({ pct: 0, current: 0, total: 0, status: 'running', phase: '시작 중' });
    try {
      const res = await pa.startNaverCategoryMap();
      naverJobIdRef.current = res.job_id;
    } catch (e) {
      setNaverProgress({ pct: 0, status: 'error', message: e.message || '네이버 매핑 시작 실패' });
    }
  }, []);

  const startCoupangMapJob = useCallback(async () => {
    setCoupangProgress({ pct: 0, current: 0, total: 0, status: 'running', phase: '시작 중' });
    try {
      const res = await pa.startCoupangCategoryMap();
      coupangJobIdRef.current = res.job_id;
    } catch (e) {
      setCoupangProgress({ pct: 0, status: 'error', message: e.message || '쿠팡 매핑 시작 실패' });
    }
  }, []);

  const totalCount = data?.total || 0;
  const unprocessedCount = data?.unprocessed_count ?? data?.items?.filter((r) => !r.ai_processed_at).length ?? 0;
  const sendableCount = data?.processed_count ?? data?.items?.filter((r) => r.ai_processed_at).length ?? 0;
  const naverPending = data?.naver_category_pending ?? 0;
  const coupangPending = data?.coupang_category_pending ?? 0;
  const naverRunning = naverProgress?.status === 'running' || naverProgress?.status === 'pending';
  const coupangRunning = coupangProgress?.status === 'running' || coupangProgress?.status === 'pending';

  const [bulkSending, setBulkSending] = useState(false);
  const [bulkSendResult, setBulkSendResult] = useState(null);

  const startBulkSend = useCallback(async () => {
    setBulkSending(true);
    setBulkSendResult(null);
    try {
      const res = await pa.bulkSendToChannel();
      setBulkSendResult(res);
    } catch (e) {
      setBulkSendResult({ error: e.message });
    } finally {
      setBulkSending(false);
      qc.invalidateQueries({ queryKey: ['pa', 'products'] });
    }
  }, [qc]);

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">상품 관리</h1>
          <p className="mt-1 text-sm text-ink-500">
            {showAll ? '전체 상품 (채널 발송 완료 포함)' : '채널에 아직 보내지 않은 상품만 표시.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-sm text-ink-600 cursor-pointer select-none pr-2">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(e) => setShowAll(e.target.checked)}
              className="rounded"
            />
            전체 상품 보기
          </label>
          {naverPending > 0 && (
            <Button
              variant="ghost"
              disabled={naverRunning}
              onClick={startNaverMapJob}
              title="products.category_path(영문)를 네이버 leaf ID로 매핑"
            >
              {naverRunning
                ? `네이버 매핑 중… ${naverProgress?.pct ?? 0}%`
                : `네이버 카테고리 매핑 (${naverPending}건)`}
            </Button>
          )}
          {coupangPending > 0 && (
            <Button
              variant="ghost"
              disabled={coupangRunning}
              onClick={startCoupangMapJob}
              title="네이버 ID → 쿠팡 카테고리 코드 매핑 (채널 보내기 이후)"
            >
              {coupangRunning
                ? `쿠팡 매핑 중… ${coupangProgress?.pct ?? 0}%`
                : `쿠팡 카테고리 매핑 (${coupangPending}건)`}
            </Button>
          )}
          {sendableCount > 0 && (
            <Button
              variant="ghost"
              disabled={bulkSending}
              onClick={startBulkSend}
            >
              {bulkSending ? '전송 중…' : `전체 채널 보내기 (${sendableCount}건)`}
            </Button>
          )}
          {unprocessedCount > 0 && (
            <Button
              variant="pa"
              disabled={batchProgress?.status === 'running'}
              onClick={() => startBatchJob({ all_unprocessed: true })}
            >
              {batchProgress?.status === 'running'
                ? `상세 생성 중… ${batchProgress.pct ?? 0}%`
                : `미처리 상세 생성 (${unprocessedCount}건)`}
            </Button>
          )}
        </div>
      </header>

      {batchProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                <span className="font-medium mr-2">[AI 상세 생성]</span>
                {batchProgress.status === 'done'
                  ? `완료 — 성공 ${batchProgress.processed}건, 실패 ${batchProgress.errors}건`
                  : batchProgress.status === 'error'
                    ? `오류: ${batchProgress.message || '알 수 없는 오류'}`
                    : `처리 중 ${batchProgress.current}/${batchProgress.total}`}
              </span>
              {batchProgress.status !== 'running' && batchProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setBatchProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-indigo-500 transition-all duration-300"
                style={{ width: `${batchProgress.pct ?? 0}%` }}
              />
            </div>
          </div>
        </Card>
      )}

      {naverProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                <span className="font-medium mr-2">[네이버 카테고리 매핑]</span>
                {naverProgress.status === 'done'
                  ? (naverProgress.phase || `완료 — 성공 ${(naverProgress.processed || 0) - (naverProgress.errors || 0)}건, 실패 ${naverProgress.errors}건`)
                  : naverProgress.status === 'error'
                    ? `오류: ${naverProgress.message || '알 수 없는 오류'}`
                    : (naverProgress.phase || `처리 중 ${naverProgress.current}/${naverProgress.total}`)}
              </span>
              {naverProgress.status !== 'running' && naverProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setNaverProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-emerald-500 transition-all duration-300"
                style={{ width: `${naverProgress.pct ?? 0}%` }}
              />
            </div>
          </div>
        </Card>
      )}

      {coupangProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                <span className="font-medium mr-2">[쿠팡 카테고리 매핑]</span>
                {coupangProgress.status === 'done'
                  ? (coupangProgress.phase || `완료 — 성공 ${(coupangProgress.processed || 0) - (coupangProgress.errors || 0)}건, 실패 ${coupangProgress.errors}건`)
                  : coupangProgress.status === 'error'
                    ? `오류: ${coupangProgress.message || '알 수 없는 오류'}`
                    : (coupangProgress.phase || `처리 중 ${coupangProgress.current}/${coupangProgress.total}`)}
              </span>
              {coupangProgress.status !== 'running' && coupangProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setCoupangProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-rose-500 transition-all duration-300"
                style={{ width: `${coupangProgress.pct ?? 0}%` }}
              />
            </div>
          </div>
        </Card>
      )}

      {bulkSendResult && (
        <Card padded>
          <div className="flex items-center justify-between text-sm">
            <span>
              {bulkSendResult.error
                ? `오류: ${bulkSendResult.error}`
                : `채널 전송 완료 — 성공 ${bulkSendResult.sent}건, 실패 ${bulkSendResult.errors}건`}
            </span>
            <Button size="sm" variant="ghost" onClick={() => setBulkSendResult(null)}>닫기</Button>
          </div>
        </Card>
      )}

      {previewHtml && (
        <Card title="상세페이지 프리뷰" padded>
          <div className="flex justify-end mb-2">
            <Button size="sm" variant="ghost" onClick={() => setPreviewHtml(null)}>닫기</Button>
          </div>
          <iframe
            srcDoc={previewHtml}
            className="w-full border rounded-lg"
            style={{ height: '600px' }}
            sandbox="allow-same-origin"
            title="detail-preview"
          />
        </Card>
      )}

      <Card title={`활성 상품 (${data?.total || 0})`} padded={false}>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={[
              ...COLS.slice(0, 3),
              {
                key: 'sale_price_krw', label: '판매가', sortable: true, width: '140px',
                render: (_, row) => <InlinePrice row={row} onSave={handlePriceSave} />,
              },
              ...COLS.slice(3),
              {
                key: 'actions',
                label: '액션',
                width: '300px',
                render: (_, row) => (
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={generatingId === row.id}
                      onClick={() => generate.mutate(row.id)}
                    >
                      {generatingId === row.id ? '처리중…' : '상세생성'}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!row.ai_processed_at}
                      onClick={() => handlePreview(row.id)}
                    >
                      프리뷰
                    </Button>
                    <Button
                      size="sm"
                      variant="pa"
                      disabled={!row.ai_processed_at || sendingId === row.id}
                      onClick={() => sendChannel.mutate(row.id)}
                    >
                      {sendingId === row.id ? '전송중…' : '채널 보내기'}
                    </Button>
                  </div>
                ),
              },
            ]}
            rows={data?.items || []}
            rowKey={(r) => r.id}
          />
        )}
      </Card>
    </div>
  );
}
