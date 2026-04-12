import React, { useState } from 'react';
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
  const [bulkResult, setBulkResult] = useState(null);

  const upload = useMutation({
    mutationFn: (pid) => pa.uploadCoupang(pid),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] }),
  });

  const uploadAll = useMutation({
    mutationFn: () => pa.uploadAllCoupang(),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] }),
    onSuccess: (res) => setBulkResult(res),
    onError: (e) => setBulkResult({ error: e.message }),
  });

  const handlePreview = async (productId) => {
    try {
      const detail = await pa.getDetailPage(productId);
      setPreviewHtml(detail.html_content || '<p>상세페이지 없음</p>');
    } catch {
      setPreviewHtml('<p>상세페이지를 불러올 수 없습니다.</p>');
    }
  };

  const pendingCount = (data?.items || []).filter((r) => r.status === 'pending').length;

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">쿠팡</h1>
          <p className="mt-1 text-sm text-ink-500">쿠팡 마켓플레이스 리스팅 관리 (수수료 13.74%).</p>
        </div>
        {pendingCount > 0 && (
          <Button
            variant="ds"
            disabled={uploadAll.isPending}
            onClick={() => { setBulkResult(null); uploadAll.mutate(); }}
          >
            {uploadAll.isPending ? '업로드 중…' : `전체 리스팅 (${pendingCount}건)`}
          </Button>
        )}
      </header>

      {bulkResult && (
        <Card padded>
          <div className="flex items-center justify-between text-sm">
            <span>
              {bulkResult.error
                ? `오류: ${bulkResult.error}`
                : `업로드 완료 — 성공 ${bulkResult.uploaded}건, 실패 ${bulkResult.errors}건`}
            </span>
            <Button size="sm" variant="ghost" onClick={() => setBulkResult(null)}>닫기</Button>
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

      <Card title={`리스팅 (${data?.items?.length || 0})`} padded={false}>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : !data?.items?.length ? (
          <div className="p-8 text-center text-sm text-ink-400">
            리스팅이 없습니다. 상품 관리에서 "채널 보내기"를 먼저 실행하세요.
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
                    <Button
                      size="sm"
                      variant="ds"
                      disabled={upload.isPending}
                      onClick={() => upload.mutate(row.product_id)}
                    >
                      업로드
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
