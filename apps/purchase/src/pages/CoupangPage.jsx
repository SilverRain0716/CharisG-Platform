import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge, LockChip } from '@charisg/ui';
import { pa } from '../api/pa.js';
import { useChannel } from '../channel.jsx';
import AttributeRecoveryTab from './coupang/AttributeRecoveryTab.jsx';

// 채널 계정 뱃지 — coupang_account: 'new'=카리스 글로벌 (A01731680), 그 외=카리스G (A00353099)
export function AccountBadge({ account }) {
  const isNew = account === 'new';
  return (
    <span
      className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${
        isNew ? 'bg-soft-info text-signal-info' : 'bg-ink-100 text-ink-500'
      }`}
      title={isNew ? '쿠팡(카리스 글로벌) A01731680' : '쿠팡(카리스G) A00353099'}
    >
      {isNew ? '카리스 글로벌' : '카리스G'}
    </span>
  );
}

const COLS = [
  { key: 'product_id', label: 'ID', width: '60px' },
  { key: 'coupang_account', label: '계정', width: '80px',
    render: (v) => <AccountBadge account={v} /> },
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
  const [previewHtml, setPreviewHtml] = useState(null);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [tab, setTab] = useState('pending');
  // 계정은 상단 계정 줄이 정한다. 화면 안에서 또 바꿀 수 있으면 상단 표시와
  // 목록 내용이 어긋난 채로 일괄 판매중지·가격변경이 나갈 수 있다.
  const { account, accountMeta } = useChannel();
  const [page, setPage] = useState(0);
  const [cursors, setCursors] = useState([null]);   // cursors[i] = before_id(이전 페이지 마지막 id) for page i
  const PAGE_SIZE = 50;
  const isList = tab === 'pending' || tab === 'listed';
  useEffect(() => { setPage(0); setCursors([null]); }, [tab, account]);   // 탭/계정 바뀌면 처음으로
  const beforeId = cursors[page] ?? null;
  const { data, isLoading } = useQuery({
    // 서버사이드 keyset 페이징 — id<before_id 커서로 깊은 페이지도 빠름 (2026-06-02)
    queryKey: ['pa', 'coupang', 'listings', tab, account, page, beforeId],
    queryFn: () => pa.coupangListings(
      isList
        ? { status: tab, limit: PAGE_SIZE, before_id: beforeId, account: account || undefined }
        : { status: 'pending', limit: 1, account: account || undefined },   // recovery 탭: totals 뱃지만
    ),
  });
  const jobIdRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const job = await pa.coupangUploadStatus(jid);
        const done = job.status === 'done' || job.status === 'error';
        setUploadProgress({
          pct: job.pct ?? 0, processed: job.processed, errors: job.errors,
          total: job.total, status: job.status, message: job.error_message,
        });
        if (done) {
          jobIdRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] });
        }
      } catch {}
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

  const handlePreview = async (productId) => {
    try {
      const detail = await pa.getDetailPage(productId);
      setPreviewHtml(detail.html_content || '<p>상세페이지 없음</p>');
    } catch {
      setPreviewHtml('<p>상세페이지를 불러올 수 없습니다.</p>');
    }
  };

  const items = data?.items || [];
  const totals = data?.totals || {};
  const pendingCount = totals.pending ?? 0;
  const listedCount = totals.listed ?? 0;
  const excludedCount = totals.excluded ?? 0;
  const filteredTotal = data?.filtered_total ?? 0;
  const totalPages = Math.max(1, Math.ceil(filteredTotal / PAGE_SIZE));
  const visibleItems = isList ? items : [];
  const goNext = () => {
    const lastId = items.length ? items[items.length - 1].id : null;
    if (lastId == null) return;
    setCursors((prev) => { const n = prev.slice(0, page + 1); n[page + 1] = lastId; return n; });
    setPage((p) => p + 1);
  };
  const goPrev = () => setPage((p) => Math.max(0, p - 1));

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">쿠팡(카리스G)</h1>
          <p className="mt-1 text-sm text-ink-500">쿠팡 마켓플레이스 리스팅 관리 (수수료 13.74%).</p>
        </div>
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
      </header>

      {uploadProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                {uploadProgress.status === 'done'
                  ? `업로드 완료 — 성공 ${uploadProgress.processed}건, 실패 ${uploadProgress.errors}건`
                  : uploadProgress.status === 'error'
                    ? `오류: ${uploadProgress.message || '알 수 없는 오류'}`
                    : `업로드 중 ${uploadProgress.processed + uploadProgress.errors}/${uploadProgress.total}`}
              </span>
              {uploadProgress.status !== 'running' && (
                <Button size="sm" variant="ghost" onClick={() => setUploadProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-signal-info transition-all duration-300"
                style={{ width: `${uploadProgress.pct ?? 0}%` }}
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
        <div className="flex items-center justify-between border-b border-ink-100 px-4">
          <div className="flex">
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
          <button
            type="button"
            onClick={() => setTab('recovery')}
            className={`px-4 py-3 text-sm font-medium border-b-2 -mb-px transition ${
              tab === 'recovery'
                ? 'border-pa-500 text-pa-600'
                : 'border-transparent text-ink-500 hover:text-ink-700'
            }`}
          >
            속성 보정 ({excludedCount})
          </button>
          </div>
          {/* 계정은 상단 계정 줄이 유일한 통제점 — 여기서는 무엇으로 잠겼는지만 보여준다 */}
          <div className="flex items-center gap-2">
            <LockChip title="상단 계정 줄에서 변경">
              {accountMeta?.label || (account === 'new' ? '카리스 글로벌' : '카리스G')}
            </LockChip>
            {accountMeta?.vendor_id && (
              <span className="font-mono text-2xs text-ink-400">{accountMeta.vendor_id}</span>
            )}
          </div>
        </div>
        {tab === 'recovery' ? (
          <div className="p-4">
            <AttributeRecoveryTab />
          </div>
        ) : isLoading ? (
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
        {isList && filteredTotal > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-ink-100 px-4 py-3 text-sm">
            <span className="text-ink-500">
              {(page * PAGE_SIZE + 1).toLocaleString()}–{Math.min((page + 1) * PAGE_SIZE, filteredTotal).toLocaleString()} / {filteredTotal.toLocaleString()}건
            </span>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="ghost" disabled={page === 0} onClick={goPrev}>이전</Button>
              <span className="px-1 text-ink-600">{page + 1} / {totalPages}</span>
              <Button size="sm" variant="ghost" disabled={page >= totalPages - 1 || items.length < PAGE_SIZE} onClick={goNext}>다음</Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
