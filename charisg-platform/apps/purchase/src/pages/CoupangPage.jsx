import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

const COLS = [
  { key: 'product_id', label: 'ID', width: '60px' },
  { key: 'title_ko', label: '상품명', wrap: true, maxWidth: '300px',
    render: (v, row) => v || row.title_en || '—' },
  { key: 'asin', label: 'ASIN', width: '120px' },
  { key: 'sale_krw', label: '판매가', width: '110px',
    render: (v) => v != null ? '\u20A9' + Number(v).toLocaleString() : '—' },
  { key: 'cost_krw_snapshot', label: '원가', width: '110px',
    render: (v) => v != null ? '\u20A9' + Number(v).toLocaleString() : '—' },
  { key: 'fee_rate', label: '수수료', width: '80px',
    render: (v) => v != null ? (v * 100).toFixed(1) + '%' : '—' },
  { key: 'net_margin_krw', label: '순마진', width: '100px',
    render: (v) => v != null ? '\u20A9' + Number(v).toLocaleString() : '—' },
  { key: 'status', label: '상태', width: '90px',
    render: (v) => (
      <StatusBadge variant={v === 'active' || v === 'listed' ? 'ok' : v === 'pending' ? 'warn' : 'neutral'}>
        {v}
      </StatusBadge>
    ) },
];

export default function CoupangPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'coupang', 'listings'],
    queryFn: pa.coupangListings,
  });
  const [previewHtml, setPreviewHtml] = useState(null);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [approvalProgress, setApprovalProgress] = useState(null);
  const [tab, setTab] = useState('pending');
  const jobIdRef = useRef(null);
  const approvalJobIdRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      if (jobIdRef.current) {
        try {
          const job = await pa.coupangUploadStatus(jobIdRef.current);
          const done = job.status === 'done' || job.status === 'error';
          setUploadProgress({
            pct: job.pct ?? 0, processed: job.processed, errors: job.errors,
            total: job.total, status: job.status,
            message: job.error_message,
            phaseMessage: job.phase_message,
          });
          if (done) {
            jobIdRef.current = null;
            qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] });
          }
        } catch {}
      }
      if (approvalJobIdRef.current) {
        try {
          const job = await pa.coupangApprovalJobStatus(approvalJobIdRef.current);
          const done = job.status === 'done' || job.status === 'error';
          setApprovalProgress({
            pct: job.pct ?? 0, processed: job.processed, errors: job.errors,
            total: job.total, status: job.status,
            message: job.error_message,
            phaseMessage: job.phase_message,
          });
          if (done) {
            approvalJobIdRef.current = null;
            qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] });
          }
        } catch {}
      }
    };
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [qc]);

  useEffect(() => {
    (async () => {
      try {
        const res = await pa.coupangUploadJob();
        if (res.job) {
          jobIdRef.current = res.job.id;
          setUploadProgress({
            pct: res.job.pct ?? 0, processed: res.job.processed, errors: res.job.errors,
            total: res.job.total, status: res.job.status,
            phaseMessage: res.job.phase_message,
          });
        }
      } catch {}
      try {
        const res = await pa.currentCoupangApprovalJob();
        if (res.job) {
          approvalJobIdRef.current = res.job.id;
          setApprovalProgress({
            pct: res.job.pct ?? 0, processed: res.job.processed, errors: res.job.errors,
            total: res.job.total, status: res.job.status,
            phaseMessage: res.job.phase_message,
          });
        }
      } catch {}
    })();
  }, []);

  const upload = useMutation({
    mutationFn: (pid) => pa.uploadCoupang(pid),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] }),
  });

  const startUploadAll = useCallback(async () => {
    setUploadProgress({ pct: 0, processed: 0, errors: 0, total: 0, status: 'running' });
    try {
      const res = await pa.uploadAllCoupang();
      jobIdRef.current = res.job_id;
    } catch (e) {
      setUploadProgress({ pct: 0, status: 'error', message: e.message || '업로드 시작 실패' });
    }
  }, []);

  const startApprovalAll = useCallback(async () => {
    setApprovalProgress({ pct: 0, processed: 0, errors: 0, total: 0, status: 'running', phaseMessage: '시작 중' });
    try {
      const res = await pa.startCoupangApproval();
      approvalJobIdRef.current = res.job_id;
    } catch (e) {
      setApprovalProgress({ pct: 0, status: 'error', message: e.message || '승인 요청 시작 실패' });
    }
  }, []);

  const handlePreview = async (productId) => {
    try {
      const detail = await pa.getDetailPage(productId);
      setPreviewHtml(detail.html_content || '<p>상세페이지 없음</p>');
    } catch {
      setPreviewHtml('<p>상세페이지를 불러올 수 없습니다.</p>');
    }
  };

  const allItems = data?.items || [];
  const pendingItems = allItems.filter((r) => r.status === 'pending');
  const listedItems = allItems.filter((r) => r.status === 'listed' || r.status === 'active');
  const pendingCount = pendingItems.length;
  const listedCount = listedItems.length;
  const visibleItems = tab === 'pending' ? pendingItems : listedItems;
  const approvalPending = data?.approval_pending ?? 0;
  const approvalRunning = approvalProgress?.status === 'running' || approvalProgress?.status === 'pending';

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">쿠팡</h1>
          <p className="mt-1 text-sm text-ink-500">쿠팡 마켓플레이스 리스팅 관리 (수수료 13.74%).</p>
        </div>
        <div className="flex gap-2">
          {approvalPending > 0 && (
            <Button
              variant="ghost"
              disabled={approvalRunning}
              onClick={startApprovalAll}
              title="임시저장(saveV2) 상태 상품에 대해 PUT /requests/approval 을 일괄 호출"
            >
              {approvalRunning
                ? `승인 요청 중… ${approvalProgress?.pct ?? 0}%`
                : `승인 요청 (${approvalPending}건)`}
            </Button>
          )}
          {pendingCount > 0 && (
            <Button
              variant="ds"
              disabled={uploadProgress?.status === 'running'}
              onClick={startUploadAll}
            >
              {uploadProgress?.status === 'running'
                ? `업로드 중… ${uploadProgress.pct ?? 0}%`
                : `전체 리스팅 (${pendingCount}건)`}
            </Button>
          )}
        </div>
      </header>

      {uploadProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                <span className="font-medium mr-2">[쿠팡 업로드]</span>
                {uploadProgress.status === 'done'
                  ? `완료 — 성공 ${uploadProgress.processed}건, 실패 ${uploadProgress.errors}건`
                  : uploadProgress.status === 'error'
                    ? `오류: ${uploadProgress.message || '알 수 없는 오류'}`
                    : `업로드 중 ${uploadProgress.processed + uploadProgress.errors}/${uploadProgress.total}`}
              </span>
              {uploadProgress.status !== 'running' && uploadProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setUploadProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-blue-500 transition-all duration-300"
                style={{ width: `${uploadProgress.pct ?? 0}%` }}
              />
            </div>
            {uploadProgress.phaseMessage && (
              <p className="text-xs text-ink-500">{uploadProgress.phaseMessage}</p>
            )}
          </div>
        </Card>
      )}

      {approvalProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                <span className="font-medium mr-2">[쿠팡 승인 요청]</span>
                {approvalProgress.status === 'done'
                  ? (approvalProgress.phaseMessage || `완료 — 성공 ${(approvalProgress.processed || 0) - (approvalProgress.errors || 0)}건, 실패 ${approvalProgress.errors}건`)
                  : approvalProgress.status === 'error'
                    ? `오류: ${approvalProgress.message || '알 수 없는 오류'}`
                    : (approvalProgress.phaseMessage || `진행 중 ${(approvalProgress.processed || 0) + (approvalProgress.errors || 0)}/${approvalProgress.total}`)}
              </span>
              {approvalProgress.status !== 'running' && approvalProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setApprovalProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-amber-500 transition-all duration-300"
                style={{ width: `${approvalProgress.pct ?? 0}%` }}
              />
            </div>
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
            title="coupang-preview"
          />
        </Card>
      )}

      <Card padded={false}>
        <div className="flex border-b border-ink-100 px-4">
          <button
            type="button"
            onClick={() => setTab('pending')}
            className={`px-4 py-3 text-sm font-medium border-b-2 -mb-px transition ${
              tab === 'pending'
                ? 'border-pa-500 text-pa-600'
                : 'border-transparent text-ink-500 hover:text-ink-700'
            }`}
          >
            업로드 대기 ({pendingCount})
          </button>
          <button
            type="button"
            onClick={() => setTab('listed')}
            className={`px-4 py-3 text-sm font-medium border-b-2 -mb-px transition ${
              tab === 'listed'
                ? 'border-pa-500 text-pa-600'
                : 'border-transparent text-ink-500 hover:text-ink-700'
            }`}
          >
            업로드 완료 ({listedCount})
          </button>
        </div>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : !visibleItems.length ? (
          <div className="p-8 text-center text-sm text-ink-400">
            {tab === 'pending'
              ? '업로드 대기 중인 리스팅이 없습니다. 상품 관리에서 "채널 보내기"를 먼저 실행하세요.'
              : '업로드 완료된 리스팅이 없습니다.'}
          </div>
        ) : (
          <DataTable
            columns={[
              ...COLS,
              {
                key: 'actions', label: '액션', width: '180px',
                render: (_, row) => (
                  <div className="flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => handlePreview(row.product_id)}>
                      프리뷰
                    </Button>
                    {tab === 'pending' && (
                      <Button
                        size="sm"
                        variant="ds"
                        disabled={upload.isPending}
                        onClick={() => upload.mutate(row.product_id)}
                      >
                        업로드
                      </Button>
                    )}
                  </div>
                ),
              },
            ]}
            rows={visibleItems}
            rowKey={(r) => r.id}
          />
        )}
      </Card>
    </div>
  );
}
