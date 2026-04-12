import React, { useState, useCallback } from 'react';
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

  const startBatch = useCallback(async () => {
    setBatchProgress({ pct: 0, current: 0, total: 0, status: 'running' });
    try {
      const res = await pa.generateDetailBatch({ all_unprocessed: true });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const payload = JSON.parse(line.slice(6));
            if (payload.event === 'done') {
              setBatchProgress({ pct: 100, ...payload, status: 'done' });
            } else {
              setBatchProgress((prev) => ({ ...prev, ...payload, status: 'running' }));
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch (e) {
      setBatchProgress((prev) => ({ ...prev, status: 'error', message: e.message }));
    } finally {
      qc.invalidateQueries({ queryKey: ['pa', 'products'] });
    }
  }, [qc]);

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
          {unprocessedCount > 0 && (
            <Button
              variant="pa"
              disabled={batchProgress?.status === 'running'}
              onClick={startBatch}
            >
              {batchProgress?.status === 'running'
                ? `AI 처리 중… ${batchProgress.pct ?? 0}%`
                : `전체 AI 처리 (${unprocessedCount}건)`}
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
                  ? `완료 — 성공 ${batchProgress.processed}건, 실패 ${batchProgress.errors}건 (${batchProgress.elapsed_sec}초)`
                  : batchProgress.status === 'error'
                    ? `오류: ${batchProgress.message}`
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
