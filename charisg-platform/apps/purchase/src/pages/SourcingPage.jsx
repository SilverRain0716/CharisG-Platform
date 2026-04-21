import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable, Input } from '@charisg/ui';
import { pa } from '../api/pa.js';

const COLS = [
  { key: 'asin', label: 'ASIN', width: '110px' },
  { key: 'title', label: '상품명', wrap: true, maxWidth: '320px' },
  { key: 'price_usd', label: '$', sortable: true, width: '80px',
    render: (v) => v != null ? '$' + Number(v).toFixed(2) : '—' },
  { key: 'rating', label: '★', width: '60px',
    render: (v) => v != null ? Number(v).toFixed(1) : '—' },
  { key: 'review_count', label: '리뷰', sortable: true, width: '80px',
    render: (v) => v != null ? Number(v).toLocaleString() : '0' },
  { key: 'monthly_sales', label: '월판매량', width: '90px',
    render: (v) => v || '—' },
  { key: 'category', label: '카테고리', wrap: true, maxWidth: '180px',
    render: (v) => v || '—' },
  { key: 'notes', label: '특이사항', wrap: true, maxWidth: '220px',
    render: (v) => v ? <span className="text-amber-700">{v}</span> : '—' },
  { key: 'collected_at', label: '수집일', width: '110px',
    render: (v) => v ? v.slice(0, 10) : '—' },
];

export default function SourcingPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [selected, setSelected] = useState([]);
  const [sheetUrl, setSheetUrl] = useState('');
  const [importMsg, setImportMsg] = useState(null);   // {tone: 'ok'|'err', text: string}

  const [promoteProgress, setPromoteProgress] = useState(null);
  const promoteJobRef = useRef(null);
  const pollRef = useRef(null);
  const navigatedRef = useRef(false);

  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'sourcing'],
    queryFn: () => pa.sourcing({ limit: 500 }),
  });

  const importMut = useMutation({
    mutationFn: () => pa.importSheet(sheetUrl.trim()),
    onSuccess: (res) => {
      const parts = (res.tabs || []).map((t) => `${t.name}: ${t.imported}건`).join(' / ');
      const dupePart = res.total_skipped > 0 ? ` (중복 ${res.total_skipped}건 skip)` : '';
      setImportMsg({ tone: 'ok', text: `${parts || '0건'} import${dupePart}` });
      setSheetUrl('');
      qc.invalidateQueries({ queryKey: ['pa', 'sourcing'] });
    },
    onError: (err) => {
      const status = err?.status || err?.response?.status;
      if (status === 403) {
        setImportMsg({
          tone: 'err',
          text: "⚠️ 시트가 공개 상태가 아닙니다. Google Sheets 에서 우상단 '공유' → '링크가 있는 모든 사용자' → '뷰어' 로 변경 후 다시 시도하세요.",
        });
      } else {
        setImportMsg({ tone: 'err', text: `import 실패: ${err?.message || '알 수 없는 오류'}` });
      }
    },
  });

  const bulkDeleteMut = useMutation({
    mutationFn: (ids) => pa.bulkDeleteCandidates(ids),
    onSuccess: (res) => {
      setSelected([]);
      setImportMsg({ tone: 'ok', text: `${res.deleted}건 삭제됨` });
      qc.invalidateQueries({ queryKey: ['pa', 'sourcing'] });
    },
    onError: (err) => setImportMsg({ tone: 'err', text: `삭제 실패: ${err?.message || ''}` }),
  });

  // 폴링: promoteJobRef 에 job_id 가 있으면 2초마다 상태 조회
  useEffect(() => {
    const poll = async () => {
      const jid = promoteJobRef.current;
      if (!jid) return;
      try {
        const job = await pa.getPromoteJobStatus(jid);
        const done = job.status === 'done' || job.status === 'error';
        setPromoteProgress({
          pct: job.pct ?? 0,
          current: (job.processed || 0) + (job.errors || 0),
          total: job.total || 0,
          processed: job.processed || 0,
          errors: job.errors || 0,
          status: job.status,
          phase: job.phase_message,
          message: job.error_message,
        });
        if (done) {
          promoteJobRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'sourcing'] });
          qc.invalidateQueries({ queryKey: ['pa', 'products'] });
          if (job.status === 'done' && !navigatedRef.current) {
            navigatedRef.current = true;
            setTimeout(() => navigate('/products'), 1500);
          }
        }
      } catch { /* 네트워크 오류 무시, 다음 폴링에서 재시도 */ }
    };
    pollRef.current = setInterval(poll, 2000);
    return () => clearInterval(pollRef.current);
  }, [qc, navigate]);

  // 페이지 진입 시 실행 중인 job 자동 감지
  useEffect(() => {
    (async () => {
      try {
        const res = await pa.getCurrentPromoteJob();
        if (res.job) {
          promoteJobRef.current = res.job.id;
          setPromoteProgress({
            pct: res.job.pct ?? 0,
            current: (res.job.processed || 0) + (res.job.errors || 0),
            total: res.job.total || 0,
            processed: res.job.processed || 0,
            errors: res.job.errors || 0,
            status: res.job.status,
            phase: res.job.phase_message,
          });
        }
      } catch { /* ignore */ }
    })();
  }, []);

  const total = data?.total || 0;

  const handleImport = () => {
    if (!sheetUrl.trim()) {
      setImportMsg({ tone: 'err', text: '시트 URL 을 입력하세요' });
      return;
    }
    setImportMsg(null);
    importMut.mutate();
  };

  const handleBulkDelete = () => {
    if (!selected.length) return;
    bulkDeleteMut.mutate(selected);
  };

  const startPromote = useCallback(async () => {
    if (!total) return;
    // 예상 시간: SP-API rate limit 0.55초/건 + 응답시간
    const etaSec = Math.ceil(total * 0.7);
    const etaText = etaSec < 60 ? `${etaSec}초` : `약 ${Math.ceil(etaSec / 60)}분`;
    if (!window.confirm(`총 ${total}개를 상품관리로 이관합니다.\nSP-API 보강에 ${etaText} 정도 걸립니다. 계속하시겠습니까?`)) return;

    setImportMsg(null);
    navigatedRef.current = false;
    setPromoteProgress({ pct: 0, current: 0, total, processed: 0, errors: 0, status: 'running', phase: '시작 중' });
    try {
      const res = await pa.startPromoteJob();
      promoteJobRef.current = res.job_id;
    } catch (e) {
      setPromoteProgress({ pct: 0, status: 'error', message: e?.message || '이관 시작 실패' });
    }
  }, [total]);

  const isPromoting = promoteProgress?.status === 'running' || promoteProgress?.status === 'pending';

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">소싱</h1>
        <p className="mt-1 text-sm text-ink-500">
          Claude 웹 프로젝트가 출력한 Google 시트 URL 을 붙여넣어 ASIN 후보를 가져옵니다. 검토 후 상품관리로 이관하세요.
        </p>
      </header>

      <Card title="Google 시트 가져오기">
        <div className="space-y-3">
          <div className="flex gap-2">
            <div className="flex-1">
              <Input
                value={sheetUrl}
                onChange={(e) => setSheetUrl(e.target.value)}
                placeholder="https://docs.google.com/spreadsheets/d/.../edit?usp=sharing"
                onKeyDown={(e) => { if (e.key === 'Enter') handleImport(); }}
              />
            </div>
            <div className="flex items-start">
              <Button variant="pa" onClick={handleImport} disabled={importMut.isPending}>
                {importMut.isPending ? '가져오는 중...' : '시트 가져오기'}
              </Button>
            </div>
          </div>
          <p className="text-xs text-ink-500">
            시트는 '링크가 있는 모든 사용자: 뷰어' 로 공개되어 있어야 합니다. 모든 탭이 자동으로 import 됩니다.
          </p>
          {importMsg && (
            <div
              className={
                'rounded-md px-3 py-2 text-sm ' +
                (importMsg.tone === 'err'
                  ? 'bg-red-50 text-red-800 border border-red-200'
                  : 'bg-emerald-50 text-emerald-800 border border-emerald-200')
              }
            >
              {importMsg.text}
            </div>
          )}
        </div>
      </Card>

      {promoteProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                {promoteProgress.status === 'done'
                  ? `완료 — 성공 ${promoteProgress.processed}건, 오류 ${promoteProgress.errors}건. 상품관리로 이동합니다…`
                  : promoteProgress.status === 'error'
                    ? `오류: ${promoteProgress.message || '알 수 없는 오류'}`
                    : `${promoteProgress.phase || '처리 중'} — ${promoteProgress.current}/${promoteProgress.total}`}
              </span>
              {promoteProgress.status !== 'running' && promoteProgress.status !== 'pending' && (
                <Button size="sm" variant="ghost" onClick={() => setPromoteProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-indigo-500 transition-all duration-300"
                style={{ width: `${promoteProgress.pct ?? 0}%` }}
              />
            </div>
          </div>
        </Card>
      )}

      <Card
        title={`소싱 후보 (${total})`}
        padded={false}
        action={
          <div className="flex gap-2 px-4">
            <Button
              variant="danger"
              size="sm"
              onClick={handleBulkDelete}
              disabled={!selected.length || bulkDeleteMut.isPending}
            >
              선택 삭제 ({selected.length})
            </Button>
            <Button
              variant="pa"
              size="sm"
              onClick={startPromote}
              disabled={!total || isPromoting}
            >
              {isPromoting
                ? `이관 중… ${promoteProgress?.pct ?? 0}%`
                : '상품관리로 전체 이관'}
            </Button>
          </div>
        }
      >
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={COLS}
            rows={data?.items || []}
            rowKey={(r) => r.id}
            selectable
            onSelect={setSelected}
          />
        )}
      </Card>
    </div>
  );
}
