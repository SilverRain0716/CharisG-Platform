import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

// 신계정(A01731680, 카리스글로벌) 신규 등록 페이지.
// 기존 대시보드의 /coupang 페이지는 pa-api 프로세스 default(COUPANG_ACTIVE=old)라
// 구계정으로 등록됨. 이 페이지는 신 엔드포인트(/api/pa/coupang-new/*)로 신계정 라우팅.

const TABS = [
  { id: 'single', label: '단품 등록' },
  { id: 'group',  label: '그룹(멀티옵션) 등록' },
  { id: 'regroup', label: '재그룹 현황' },
];

export default function NewAccountUploadPage() {
  const [tab, setTab] = useState('single');

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">
          쿠팡(카리스 글로벌)
        </h1>
        <p className="mt-1 text-sm text-ink-500">
          Vendor A01731680 — 신규 상품을 카리스 글로벌 계정으로 등록. 기존{' '}
          <code className="text-xs bg-ink-100 px-1 rounded">쿠팡(카리스G)</code> 페이지는 카리스G 계정(A00353099) 흐름 그대로 유지됩니다.
        </p>
      </header>

      <Card padded={false}>
        <div className="flex border-b border-ink-100 px-4">
          {TABS.map((t) => (
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
        </div>
        <div className="p-4">
          {tab === 'single'  && <SingleUploadTab />}
          {tab === 'group'   && <GroupUploadTab />}
          {tab === 'regroup' && <RegroupStatusTab />}
        </div>
      </Card>
    </div>
  );
}

// ────────── 공통: job 폴링 훅 ──────────

function useUploadJob() {
  const qc = useQueryClient();
  const [progress, setProgress] = useState(null);
  const jobIdRef = useRef(null);

  useEffect(() => {
    const poll = async () => {
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const job = await pa.newAccountUploadJob(jid);
        const done = job.status === 'done' || job.status === 'error';
        setProgress({
          pct: job.pct ?? 0,
          status: job.status,
          processed: job.processed,
          total: job.total,
          phase_message: job.phase_message,
          error_message: job.error_message,
        });
        if (done) {
          jobIdRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'listings'] });
        }
      } catch {
        // 인증 만료 등 — 계속 폴링해도 무해
      }
    };
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [qc]);

  const start = useCallback((jobId) => {
    jobIdRef.current = jobId;
    setProgress({ pct: 0, status: 'pending', phase_message: '대기 중' });
  }, []);

  const reset = useCallback(() => {
    jobIdRef.current = null;
    setProgress(null);
  }, []);

  return { progress, start, reset, isRunning: !!jobIdRef.current };
}

// ────────── 공통: 진행률 카드 ──────────

function ProgressCard({ progress, onClose }) {
  if (!progress) return null;
  const { status, pct, phase_message, error_message } = progress;
  const running = status === 'pending' || status === 'running';
  return (
    <Card padded className="mt-4">
      <div className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span>
            {status === 'done'
              ? `완료 — ${phase_message || ''}`
              : status === 'error'
              ? `오류: ${error_message || phase_message || '알 수 없는 오류'}`
              : `진행중… ${phase_message || ''}`}
          </span>
          {!running && (
            <Button size="sm" variant="ghost" onClick={onClose}>닫기</Button>
          )}
        </div>
        <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-300 ${
              status === 'error' ? 'bg-signal-err' : 'bg-signal-info'
            }`}
            style={{ width: `${pct ?? 0}%` }}
          />
        </div>
      </div>
    </Card>
  );
}

// ────────── 탭 1: 단품 등록 ──────────

function SingleUploadTab() {
  const [asin, setAsin] = useState('');
  const { progress, start, reset, isRunning } = useUploadJob();

  const upload = useMutation({
    mutationFn: (v) => pa.newAccountUploadSingle({ asin: v }),
    onSuccess: (res) => start(res.job_id),
  });

  const disabled = isRunning || upload.isPending || !asin.trim();
  const running = progress?.status === 'pending' || progress?.status === 'running';

  return (
    <div className="space-y-3">
      <div>
        <label className="block text-sm font-medium text-ink-700 mb-1">
          ASIN (아마존 상품 코드)
        </label>
        <div className="flex gap-2">
          <input
            type="text"
            value={asin}
            onChange={(e) => setAsin(e.target.value.toUpperCase())}
            placeholder="예: B0DX964WJR"
            disabled={running}
            className="flex-1 rounded-md border border-ink-200 px-3 py-2 text-sm focus:border-pa-500 focus:outline-none focus:ring-1 focus:ring-pa-500 disabled:bg-ink-50 disabled:text-ink-400"
          />
          <Button
            variant="pa"
            disabled={disabled}
            onClick={() => upload.mutate(asin.trim())}
          >
            {running ? '등록 중…' : '단품 등록'}
          </Button>
        </div>
        <p className="mt-1 text-xs text-ink-500">
          파이프라인: SP-API facts → cost 책정 → AI 한글화 → 카테고리·가격 → 신계정 즉시 판매요청.
          약 30초~2분 소요.
        </p>
      </div>
      <ProgressCard progress={progress} onClose={reset} />
    </div>
  );
}

// ────────── 탭 2: 그룹 등록 ──────────

function GroupUploadTab() {
  const [parentAsin, setParentAsin] = useState('');
  const [oldCpid, setOldCpid] = useState('');
  const { progress, start, reset, isRunning } = useUploadJob();

  const upload = useMutation({
    mutationFn: (body) => pa.newAccountUploadGroup(body),
    onSuccess: (res) => start(res.job_id),
  });

  const disabled = isRunning || upload.isPending || !parentAsin.trim();
  const running = progress?.status === 'pending' || progress?.status === 'running';

  return (
    <div className="space-y-3">
      <div>
        <label className="block text-sm font-medium text-ink-700 mb-1">
          PARENT ASIN (변형 그룹 부모)
        </label>
        <input
          type="text"
          value={parentAsin}
          onChange={(e) => setParentAsin(e.target.value.toUpperCase())}
          placeholder="예: B0F8BN888P"
          disabled={running}
          className="w-full rounded-md border border-ink-200 px-3 py-2 text-sm focus:border-pa-500 focus:outline-none focus:ring-1 focus:ring-pa-500 disabled:bg-ink-50 disabled:text-ink-400"
        />
      </div>
      <div>
        <label className="block text-sm font-medium text-ink-700 mb-1">
          기존 단품 쿠팡ID <span className="text-ink-400 font-normal">(선택 — 있으면 판매중지 후 그룹으로 승격)</span>
        </label>
        <input
          type="text"
          value={oldCpid}
          onChange={(e) => setOldCpid(e.target.value.replace(/[^0-9]/g, ''))}
          placeholder="예: 16305364759 (없으면 비워두세요)"
          disabled={running}
          className="w-full rounded-md border border-ink-200 px-3 py-2 text-sm focus:border-pa-500 focus:outline-none focus:ring-1 focus:ring-pa-500 disabled:bg-ink-50 disabled:text-ink-400"
        />
      </div>
      <div className="flex justify-end">
        <Button
          variant="pa"
          disabled={disabled}
          onClick={() => upload.mutate({
            parent_asin: parentAsin.trim(),
            old_cpid: oldCpid.trim() || undefined,
          })}
        >
          {running ? '등록 중…' : '그룹 등록'}
        </Button>
      </div>
      <p className="text-xs text-ink-500">
        파이프라인: (선택) 기존 단품 판매중지 → SP-API 자식 재발굴 → AI detailing → 옵션별 이미지 →
        쿠팡 멀티옵션 등록. 약 1~5분 소요.
      </p>
      <ProgressCard progress={progress} onClose={reset} />
    </div>
  );
}

// ────────── 탭 3: 재그룹 현황 ──────────

function RegroupStatusTab() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['pa', 'coupang-new', 'regroup-status'],
    queryFn: () => pa.newAccountRegroupStatus(),
    staleTime: 30_000,
  });

  if (isLoading) return <div className="text-sm text-ink-500">로딩 중…</div>;
  if (!data) return <div className="text-sm text-signal-err">응답 오류</div>;

  const rs = data.regroup_scan || {};
  const total = (rs.parent || 0) + (rs.child || 0) + (rs.single || 0) + (rs.fail || 0);

  return (
    <div className="space-y-4">
      {!data.regroup_db_exists && (
        <div className="rounded border border-signal-warn/40 bg-soft-warn p-3 text-sm text-signal-warn">
          regroup.db 파일이 아직 생성되지 않았습니다. SSH에서{' '}
          <code className="text-xs bg-soft-warn px-1 rounded">python /home/ubuntu/re_group_existing.py scan</code>{' '}
          를 실행하면 시작됩니다.
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="스캔 총합" value={total} />
        <StatCard label="부모(parent)" value={rs.parent || 0} />
        <StatCard label="자식(child)" value={rs.child || 0} />
        <StatCard label="단독(single)" value={rs.single || 0} />
        <StatCard label="판매중 자식" value={data.selling_children || 0} accent="green" />
        <StatCard label="미스캔 잔여" value={data.unscanned || 0} accent="yellow" />
        <StatCard label="제외목록" value={data.exclude_count || 0} />
        <StatCard label="재그룹 큐" value={data.regroup_queued || 0} accent="blue" />
      </div>

      <div className="flex justify-between items-center pt-2 border-t border-ink-100">
        <div className="text-xs text-ink-500 space-y-1">
          <div>
            <strong>실행은 SSH로만 가능</strong> — 상태 조회는 이 페이지, 실행은 서버 CLI로 분리되어 있음
          </div>
          <div className="font-mono text-[11px]">
            python /home/ubuntu/re_group_existing.py <em>scan</em>|<em>enqueue --apply</em>|<em>exclude B0XXX</em>|<em>status</em>
          </div>
        </div>
        <Button size="sm" variant="ghost" onClick={() => refetch()} disabled={isFetching}>
          {isFetching ? '새로고침 중…' : '새로고침'}
        </Button>
      </div>

      {data.error && (
        <div className="rounded border border-signal-err/40 bg-soft-err p-3 text-sm text-signal-err">
          {data.error}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, accent }) {
  const accentClass =
    accent === 'green' ? 'text-signal-ok' :
    accent === 'yellow' ? 'text-signal-warn' :
    accent === 'blue' ? 'text-signal-info' :
    'text-ink-900';
  return (
    <div className="rounded-lg border border-ink-100 bg-surface p-3">
      <div className="text-xs text-ink-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${accentClass}`}>
        {Number(value).toLocaleString()}
      </div>
    </div>
  );
}
