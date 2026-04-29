import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

/**
 * batch_jobs.phase 코드 → UI 메타.
 * - label: 배지에 표기될 단계명
 * - badge: tailwind 클래스 (배지 배경/텍스트)
 * - bar:   진행바 색상 (Phase 1·1.5는 처리 카운터가 0이라 폭이 0이지만, 단계 진입 자체를
 *          가시화하기 위해 보조용으로 사용)
 */
const PHASE_META = {
  phase_1:   { label: '1단계 · 이미지 업로드',  badge: 'bg-blue-100 text-blue-700',     bar: 'bg-blue-500' },
  phase_1_5: { label: '1.5단계 · 속성 추론',    badge: 'bg-violet-100 text-violet-700', bar: 'bg-violet-500' },
  phase_2:   { label: '2단계 · 상품 등록',      badge: 'bg-emerald-100 text-emerald-700', bar: 'bg-emerald-500' },
  done:      { label: '완료',                   badge: 'bg-ink-100 text-ink-700',       bar: 'bg-ink-400' },
};

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

/* ── 속성 수정 탭 컴포넌트 ──────────────────────── */
function AttributesTab() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'smartstore', 'attr-pending'],
    queryFn: pa.attrPending,
  });
  const [selectedPid, setSelectedPid] = useState(null);
  const [attrData, setAttrData] = useState(null);
  const [inferResult, setInferResult] = useState(null);
  const [inferring, setInferring] = useState(false);
  const [saving, setSaving] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchResult, setBatchResult] = useState(null);

  // 속성값 선택 상태 (attributeSeq → attributeValueSeq)
  const [selections, setSelections] = useState({});

  const handleSelect = async (pid) => {
    setSelectedPid(pid);
    setInferResult(null);
    setSelections({});
    try {
      const res = await pa.attrGet(pid);
      setAttrData(res);
      // 이미 입력된 값 로드
      const initial = {};
      for (const attr of res.attributes) {
        if (attr.currentValueSeq) initial[attr.attributeSeq] = attr.currentValueSeq;
      }
      setSelections(initial);
    } catch (e) {
      setAttrData(null);
    }
  };

  const handleInfer = async () => {
    if (!selectedPid) return;
    setInferring(true);
    try {
      const res = await pa.attrInfer(selectedPid);
      setInferResult(res);
      // AI 추론 결과를 selections에 반영 (이미 수동 선택된 것은 유지)
      const updated = { ...selections };
      for (const attr of res.inferred) {
        if (attr.inferredValueSeq && !updated[attr.attributeSeq]) {
          updated[attr.attributeSeq] = attr.inferredValueSeq;
        }
      }
      setSelections(updated);
    } catch (e) {
      alert('AI 추론 실패: ' + (e.message || '알 수 없는 오류'));
    } finally {
      setInferring(false);
    }
  };

  const handleSave = async () => {
    if (!selectedPid) return;
    setSaving(true);
    try {
      const attrs = Object.entries(selections)
        .filter(([, vseq]) => vseq)
        .map(([aseq, vseq]) => ({ attributeSeq: Number(aseq), attributeValueSeq: Number(vseq) }));
      await pa.attrSave(selectedPid, attrs);
      qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'attr-pending'] });
      setSelectedPid(null);
      setAttrData(null);
      setInferResult(null);
      setSelections({});
    } catch (e) {
      alert('저장 실패: ' + (e.message || '알 수 없는 오류'));
    } finally {
      setSaving(false);
    }
  };

  const [batchAllProgress, setBatchAllProgress] = useState(null);
  const pollRef = useRef(null);

  const handleBatchAll = async () => {
    setBatchRunning(true);
    setBatchResult(null);
    setBatchAllProgress(null);
    try {
      const res = await pa.attrBatchAll();
      if (!res.ok && res.error) {
        setBatchResult({ error: res.error });
        setBatchRunning(false);
        return;
      }
      // 진행률 폴링 시작
      pollRef.current = setInterval(async () => {
        try {
          const status = await pa.attrBatchAllStatus();
          setBatchAllProgress(status);
          if (!status.running) {
            clearInterval(pollRef.current);
            pollRef.current = null;
            setBatchRunning(false);
            setBatchResult({
              results: [
                ...Array(status.processed).fill({ ok: true }),
                ...Array(status.errors).fill({ ok: false }),
              ],
            });
            qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'attr-pending'] });
          }
        } catch { /* ignore */ }
      }, 3000);
    } catch (e) {
      setBatchResult({ error: e.message });
      setBatchRunning(false);
    }
  };

  // 마운트 시 진행 중인 작업 자동 감지 + 폴링 복구
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await pa.attrBatchAllStatus();
        if (!cancelled && status.running) {
          setBatchRunning(true);
          setBatchAllProgress(status);
          // 폴링 시작
          pollRef.current = setInterval(async () => {
            try {
              const s = await pa.attrBatchAllStatus();
              setBatchAllProgress(s);
              if (!s.running) {
                clearInterval(pollRef.current);
                pollRef.current = null;
                setBatchRunning(false);
                setBatchResult({
                  results: [
                    ...Array(s.processed).fill({ ok: true }),
                    ...Array(s.errors).fill({ ok: false }),
                  ],
                });
                qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'attr-pending'] });
              }
            } catch { /* ignore */ }
          }, 3000);
        }
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  const pending = data?.pending ?? 0;
  const done = data?.done ?? 0;
  const items = data?.items ?? [];

  return (
    <div className="space-y-4">
      {/* 상단 요약 + 일괄 버튼 */}
      <div className="flex items-center justify-between px-4 py-3">
        <div className="text-sm text-ink-600">
          속성 미입력 <span className="font-semibold text-ink-900">{pending}</span>건
          {' / '}완료 <span className="font-semibold text-green-600">{done}</span>건
        </div>
        {pending > 0 && (
          <Button
            variant="pa"
            size="sm"
            disabled={batchRunning}
            onClick={handleBatchAll}
          >
            {batchRunning ? 'AI 전체 처리 중…' : `AI 전체 자동 채우기 (${pending}건)`}
          </Button>
        )}
      </div>

      {batchRunning && batchAllProgress && (
        <div className="mx-4 p-3 bg-pa-50 rounded-lg text-sm">
          <div className="flex justify-between mb-1">
            <span>처리 중... {batchAllProgress.processed + batchAllProgress.errors} / {batchAllProgress.total}</span>
            <span className="text-ink-500">
              성공 {batchAllProgress.processed} · 실패 {batchAllProgress.errors}
            </span>
          </div>
          <div className="w-full bg-ink-200 rounded-full h-2">
            <div
              className="bg-pa-500 h-2 rounded-full transition-all"
              style={{ width: `${batchAllProgress.total ? ((batchAllProgress.processed + batchAllProgress.errors) / batchAllProgress.total * 100) : 0}%` }}
            />
          </div>
        </div>
      )}

      {!batchRunning && batchResult && (
        <div className="mx-4 p-3 bg-ink-50 rounded-lg text-sm">
          {batchResult.error
            ? <span className="text-red-600">오류: {batchResult.error}</span>
            : <span>
                처리 완료 — 성공 {batchResult.results?.filter(r => r.ok).length}건,
                실패 {batchResult.results?.filter(r => !r.ok).length}건
              </span>
          }
          <button onClick={() => setBatchResult(null)} className="ml-2 text-ink-400 hover:text-ink-600">닫기</button>
        </div>
      )}

      <div className="flex gap-4 px-4">
        {/* 왼쪽: 상품 목록 */}
        <div className="w-1/3 max-h-[600px] overflow-y-auto border rounded-lg">
          {isLoading ? (
            <div className="p-4 text-center text-sm text-ink-400">로딩 중...</div>
          ) : !items.length ? (
            <div className="p-4 text-center text-sm text-ink-400">속성 수정이 필요한 상품이 없습니다.</div>
          ) : (
            items.map((item) => (
              <button
                key={item.product_id}
                type="button"
                onClick={() => handleSelect(item.product_id)}
                className={`w-full text-left px-3 py-2.5 border-b border-ink-50 text-sm hover:bg-ink-50 transition ${
                  selectedPid === item.product_id ? 'bg-pa-50 border-l-2 border-l-pa-500' : ''
                }`}
              >
                <div className="font-medium text-ink-800 truncate">{item.title_ko || '—'}</div>
                <div className="text-xs text-ink-400 mt-0.5">ID {item.product_id} · 카테고리 {item.category_path}</div>
              </button>
            ))
          )}
        </div>

        {/* 오른쪽: 속성 편집 */}
        <div className="flex-1 border rounded-lg p-4 max-h-[600px] overflow-y-auto">
          {!selectedPid ? (
            <div className="text-center text-sm text-ink-400 py-12">
              왼쪽에서 상품을 선택하세요
            </div>
          ) : !attrData ? (
            <div className="text-center text-sm text-ink-400 py-12">로딩 중...</div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-ink-900 truncate">{attrData.title}</h3>
                <div className="flex gap-2 flex-shrink-0">
                  <Button size="sm" variant="ghost" disabled={inferring} onClick={handleInfer}>
                    {inferring ? 'AI 추론 중…' : 'AI 자동 채우기'}
                  </Button>
                  <Button size="sm" variant="pa" disabled={saving} onClick={handleSave}>
                    {saving ? '저장 중…' : '네이버에 저장'}
                  </Button>
                </div>
              </div>
              <div className="text-xs text-ink-400">카테고리: {attrData.category_id}</div>

              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-ink-100 text-ink-500">
                    <th className="text-left py-2 w-28">속성</th>
                    <th className="text-left py-2 w-24">수정 전</th>
                    <th className="text-left py-2">수정 후</th>
                  </tr>
                </thead>
                <tbody>
                  {attrData.attributes.map((attr) => {
                    const inferred = inferResult?.inferred?.find(a => a.attributeSeq === attr.attributeSeq);
                    const currentName = attr.values.find(v => v.seq === attr.currentValueSeq)?.name;
                    const selectedSeq = selections[attr.attributeSeq];

                    return (
                      <tr key={attr.attributeSeq} className="border-b border-ink-50">
                        <td className="py-2 font-medium text-ink-700">
                          {attr.attributeName}
                          {attr.required && <span className="text-red-500 ml-0.5">*</span>}
                        </td>
                        <td className="py-2">
                          {currentName
                            ? <span className="text-ink-600">{currentName}</span>
                            : <span className="text-red-500 text-xs">미등록</span>
                          }
                        </td>
                        <td className="py-2">
                          <select
                            value={selectedSeq || ''}
                            onChange={(e) => setSelections(prev => ({
                              ...prev,
                              [attr.attributeSeq]: e.target.value ? Number(e.target.value) : null,
                            }))}
                            className={`w-full rounded border px-2 py-1 text-sm outline-none focus:border-pa-500 ${
                              inferred?.inferredValueSeq && !attr.currentValueSeq
                                ? 'border-green-300 bg-green-50'
                                : 'border-ink-200'
                            }`}
                          >
                            <option value="">선택</option>
                            {attr.values.map((v) => (
                              <option key={v.seq} value={v.seq}>{v.name}</option>
                            ))}
                          </select>
                          {inferred?.inferredValue && !attr.currentValueSeq && (
                            <div className="text-xs text-green-600 mt-0.5">AI: {inferred.inferredValue}</div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── 메인 페이지 ──────────────────────────────── */
export default function SmartStorePage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'smartstore', 'listings'],
    queryFn: pa.smartstoreListings,
  });
  const [previewHtml, setPreviewHtml] = useState(null);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [tab, setTab] = useState('pending');
  const jobIdRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const job = await pa.smartstoreUploadStatus(jid);
        const done = job.status === 'done' || job.status === 'error';
        setUploadProgress({
          pct: job.pct ?? 0, processed: job.processed, errors: job.errors,
          total: job.total, status: job.status,
          message: job.error_message,
          phaseMessage: job.phase_message,
          phase: job.phase,
        });
        if (done) {
          jobIdRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'listings'] });
        }
      } catch {}
    };
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [qc]);

  useEffect(() => {
    (async () => {
      try {
        const res = await pa.smartstoreUploadJob();
        if (res.job) {
          jobIdRef.current = res.job.id;
          setUploadProgress({
            pct: res.job.pct ?? 0, processed: res.job.processed, errors: res.job.errors,
            total: res.job.total, status: res.job.status,
            phaseMessage: res.job.phase_message,
            phase: res.job.phase,
          });
        }
      } catch {}
    })();
  }, []);

  const upload = useMutation({
    mutationFn: (pid) => pa.uploadSmartstore(pid),
    onSettled: () => qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'listings'] }),
  });

  const startUploadAll = useCallback(async () => {
    setUploadProgress({ pct: 0, processed: 0, errors: 0, total: 0, status: 'running' });
    try {
      const res = await pa.uploadAllSmartstore();
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

  const allItems = data?.items || [];
  const pendingItems = allItems.filter((r) => r.status === 'pending');
  const listedItems = allItems.filter((r) => r.status === 'listed' || r.status === 'active');
  const failedItems = allItems.filter((r) => r.status === 'excluded');
  const pendingCount = pendingItems.length;
  const listedCount = listedItems.length;
  const failedCount = failedItems.length;
  const visibleItems =
    tab === 'pending' ? pendingItems :
    tab === 'listed' ? listedItems :
    tab === 'failed' ? failedItems : [];

  const bulkDelete = useMutation({
    mutationFn: () => pa.bulkDeleteProducts({ channel: 'smartstore', status: 'excluded' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'listings'] }),
  });
  const deleteOne = useMutation({
    mutationFn: (pid) => pa.bulkDeleteProducts({ ids: [pid] }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'listings'] }),
  });

  const tabs = [
    { id: 'pending', label: `업로드 대기 (${pendingCount})` },
    { id: 'listed', label: `업로드 완료 (${listedCount})` },
    { id: 'failed', label: `업로드 실패 (${failedCount})` },
    { id: 'attributes', label: '속성 수정' },
  ];

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">스마트스토어</h1>
          <p className="mt-1 text-sm text-ink-500">네이버 스마트스토어 리스팅 관리 (수수료 5.48%).</p>
        </div>
        {tab === 'pending' && pendingCount > 0 && (
          <Button
            variant="pa"
            disabled={uploadProgress?.status === 'running'}
            onClick={startUploadAll}
          >
            {uploadProgress?.status === 'running'
              ? `업로드 중… ${uploadProgress.pct ?? 0}%`
              : `전체 리스팅 (${pendingCount}건)`}
          </Button>
        )}
      </header>

      {uploadProgress && (() => {
        const meta = PHASE_META[uploadProgress.phase] || null;
        // Phase 1·1.5 동안에는 processed 카운터가 0이라 pct=0이 된다.
        // 단계 진입 자체를 시각화하기 위해 사전 단계에 최소 폭을 부여한다.
        const PHASE_FLOOR = { phase_1: 5, phase_1_5: 35, phase_2: 65, done: 100 };
        const floor = PHASE_FLOOR[uploadProgress.phase] ?? 0;
        const pct = Math.max(uploadProgress.pct ?? 0, floor);
        const barClass = meta?.bar || 'bg-green-500';
        return (
          <Card padded>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  {meta && (
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${meta.badge}`}>
                      {meta.label}
                    </span>
                  )}
                  <span>
                    {uploadProgress.status === 'done'
                      ? `업로드 완료 — 성공 ${uploadProgress.processed}건, 실패 ${uploadProgress.errors}건`
                      : uploadProgress.status === 'error'
                        ? `오류: ${uploadProgress.message || '알 수 없는 오류'}`
                        : `업로드 중 ${uploadProgress.processed + uploadProgress.errors}/${uploadProgress.total}`}
                  </span>
                </div>
                {uploadProgress.status !== 'running' && (
                  <Button size="sm" variant="ghost" onClick={() => setUploadProgress(null)}>닫기</Button>
                )}
              </div>
              <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-300 ${barClass}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              {uploadProgress.phaseMessage && (
                <p className="text-xs text-ink-500">{uploadProgress.phaseMessage}</p>
              )}
            </div>
          </Card>
        );
      })()}

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
            title="smartstore-preview"
          />
        </Card>
      )}

      <Card padded={false}>
        <div className="flex border-b border-ink-100 px-4">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={`px-4 py-3 text-sm font-medium border-b-2 -mb-px transition ${
                tab === t.id
                  ? 'border-pa-500 text-pa-600'
                  : 'border-transparent text-ink-500 hover:text-ink-700'
              }`}
            >
              {t.label}
            </button>
          ))}
          {tab === 'failed' && failedCount > 0 && (
            <div className="ml-auto py-2">
              <Button
                size="sm"
                variant="pa"
                disabled={bulkDelete.isPending}
                onClick={() => {
                  if (confirm(`실패 상품 ${failedCount}건을 완전 삭제합니다.`)) {
                    bulkDelete.mutate();
                  }
                }}
              >
                {bulkDelete.isPending ? '삭제 중…' : `실패 상품 ${failedCount}건 전부 삭제`}
              </Button>
            </div>
          )}
        </div>

        {tab === 'attributes' ? (
          <AttributesTab />
        ) : isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : !visibleItems.length ? (
          <div className="p-8 text-center text-sm text-ink-400">
            {tab === 'pending'
              ? '업로드 대기 중인 리스팅이 없습니다.'
              : tab === 'listed'
                ? '업로드 완료된 리스팅이 없습니다.'
                : '업로드 실패한 리스팅이 없습니다.'}
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
                      <Button size="sm" variant="pa" disabled={upload.isPending} onClick={() => upload.mutate(row.product_id)}>
                        업로드
                      </Button>
                    )}
                    {tab === 'listed' && row.channel_product_id && (
                      <a
                        href={`https://smartstore.naver.com/main/products/${row.channel_product_id}`}
                        target="_blank" rel="noreferrer"
                        className="text-xs text-pa-600 hover:underline px-2 py-1"
                      >네이버 열기</a>
                    )}
                    {tab === 'failed' && (
                      <Button size="sm" variant="ghost" disabled={deleteOne.isPending}
                        onClick={() => { if (confirm(`상품 ${row.product_id} 삭제?`)) deleteOne.mutate(row.product_id); }}>
                        삭제
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
