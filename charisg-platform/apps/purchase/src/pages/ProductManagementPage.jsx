import React, { useState, useCallback, useEffect, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const COLS = [
  { key: 'id', label: 'ID', width: '60px' },
  { key: 'title_ko', label: '상품명', wrap: true, maxWidth: '360px',
    render: (v, row) => v || <span className="text-ink-400">{row.title_en || '—'}</span> },
  { key: 'seo_title', label: 'SEO 제목', wrap: true, maxWidth: '200px',
    render: (v) => v || <span className="text-ink-300">—</span> },
  { key: 'sale_price_krw', label: '판매가', sortable: true, width: '110px',
    render: (v) => v != null ? '₩' + Number(v).toLocaleString() : '—' },
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
  const { data, isLoading } = useQuery({ queryKey: ['pa', 'products'], queryFn: () => pa.products({ limit: 200 }) });

  const [batchProgress, setBatchProgress] = useState(null);
  const [generatingId, setGeneratingId] = useState(null);
  const [previewHtml, setPreviewHtml] = useState(null);
  const jobIdRef = useRef(null);
  const pollRef = useRef(null);

  // 폴링: jobIdRef에 job_id가 있으면 2초마다 상태 조회
  useEffect(() => {
    const poll = async () => {
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const job = await pa.getBatchJobStatus(jid);
        const done = job.status === 'done' || job.status === 'error';
        setBatchProgress({
          pct: job.pct ?? 0,
          current: job.processed + job.errors,
          total: job.total,
          processed: job.processed,
          errors: job.errors,
          status: job.status,
          message: job.error_message,
        });
        if (done) {
          jobIdRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'products'] });
        }
      } catch { /* 네트워크 오류 무시, 다음 폴링에서 재시도 */ }
    };

    pollRef.current = setInterval(poll, 2000);
    return () => clearInterval(pollRef.current);
  }, [qc]);

  // 페이지 진입 시 실행 중인 job 자동 감지
  useEffect(() => {
    (async () => {
      try {
        const res = await pa.getCurrentBatchJob();
        if (res.job) {
          jobIdRef.current = res.job.id;
          setBatchProgress({
            pct: res.job.pct ?? 0,
            current: res.job.processed + res.job.errors,
            total: res.job.total,
            processed: res.job.processed,
            errors: res.job.errors,
            status: res.job.status,
          });
        }
      } catch { /* ignore */ }
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

  const totalCount = data?.items?.length || 0;
  const unprocessedCount = data?.items?.filter((r) => !r.ai_processed_at).length || 0;
  const sendableCount = data?.items?.filter((r) => r.ai_processed_at).length || 0;

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
          <p className="mt-1 text-sm text-ink-500">상세페이지 → 등록 → 활성 상품 라이프사이클.</p>
        </div>
        <div className="flex gap-2">
          {sendableCount > 0 && (
            <Button
              variant="ghost"
              disabled={bulkSending}
              onClick={startBulkSend}
            >
              {bulkSending ? '전송 중…' : `전체 채널 보내기 (${sendableCount}건)`}
            </Button>
          )}
          {totalCount > 0 && (
            <Button
              variant="pa"
              disabled={batchProgress?.status === 'running'}
              onClick={() => startBatchJob({ all_products: true })}
            >
              {batchProgress?.status === 'running'
                ? `상세 생성 중… ${batchProgress.pct ?? 0}%`
                : `전체 상세 생성 (${totalCount}건${unprocessedCount > 0 ? `, 미처리 ${unprocessedCount}` : ''})`}
            </Button>
          )}
        </div>
      </header>

      {batchProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                {batchProgress.status === 'done'
                  ? `완료 — 성공 ${batchProgress.processed}건, 실패 ${batchProgress.errors}건`
                  : batchProgress.status === 'error'
                    ? `오류: ${batchProgress.message || '알 수 없는 오류'}`
                    : `처리 중 ${batchProgress.current}/${batchProgress.total}`}
              </span>
              {batchProgress.status !== 'running' && (
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
              ...COLS,
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
