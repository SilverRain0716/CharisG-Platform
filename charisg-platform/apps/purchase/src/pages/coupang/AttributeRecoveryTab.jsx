import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card, Button, DataTable } from '@charisg/ui';
import { pa } from '../../api/pa.js';

/**
 * 쿠팡 MANDATORY 속성 부족으로 excluded 된 상품을 복구하는 탭.
 *  - 누락 속성별 필터 chip
 *  - 행별 인라인 수동값 입력
 *  - 벌크 액션 3개: AI 엄격 재추출 / 수동 저장+복구 / 복구만
 *  - reextract-strict 진행률 폴링
 */
export default function AttributeRecoveryTab() {
  const qc = useQueryClient();
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['pa', 'coupang', 'excluded'],
    queryFn: pa.coupangExcluded,
  });

  const [attrFilter, setAttrFilter] = useState('ALL');
  const [selected, setSelected] = useState(() => new Set());
  const [edits, setEdits] = useState({});  // {pid: {attrName: value}}
  const [reextractProgress, setReextractProgress] = useState(null);
  const [statusMsg, setStatusMsg] = useState(null);
  const jobIdRef = useRef(null);
  const [busy, setBusy] = useState(false);

  const items = data?.items || [];
  const byAttr = data?.by_attr || {};

  const filteredItems = useMemo(() => {
    if (attrFilter === 'ALL') return items;
    return items.filter((r) => r.missing_attr === attrFilter);
  }, [items, attrFilter]);

  // reextract job 폴링
  useEffect(() => {
    const poll = async () => {
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const job = await pa.coupangReextractStatus(jid);
        const done = job.status === 'done' || job.status === 'error';
        setReextractProgress({
          pct: job.pct ?? 0, processed: job.processed, errors: job.errors,
          total: job.total, status: job.status, message: job.phase_message,
        });
        if (done) {
          jobIdRef.current = null;
          qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'excluded'] });
        }
      } catch {}
    };
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [qc]);

  const toggleOne = (pid) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(pid)) n.delete(pid); else n.add(pid);
      return n;
    });
  };
  const toggleAllVisible = () => {
    const allIds = filteredItems.map((r) => r.product_id);
    setSelected((prev) => {
      const allSelected = allIds.every((id) => prev.has(id));
      const n = new Set(prev);
      if (allSelected) allIds.forEach((id) => n.delete(id));
      else allIds.forEach((id) => n.add(id));
      return n;
    });
  };

  const updateEdit = (pid, attrName, value) => {
    setEdits((prev) => ({
      ...prev,
      [pid]: { ...(prev[pid] || {}), [attrName]: value },
    }));
  };

  const selectedPids = useMemo(() => [...selected], [selected]);

  const runReextract = useCallback(async () => {
    if (selectedPids.length === 0) return;
    setBusy(true);
    setStatusMsg(null);
    try {
      const res = await pa.coupangReextractStrict({ product_ids: selectedPids });
      jobIdRef.current = res.job_id;
      setReextractProgress({
        pct: 0, processed: 0, errors: 0, total: res.total, status: 'running',
      });
    } catch (e) {
      setStatusMsg({ type: 'error', text: e.message || 'AI 재추출 시작 실패' });
    }
    setBusy(false);
  }, [selectedPids]);

  const runSaveAndRestore = useCallback(async () => {
    if (selectedPids.length === 0) return;
    setBusy(true);
    setStatusMsg(null);
    // edits 에서 선택된 pid 의 값만 수집
    const bulkItems = selectedPids
      .map((pid) => ({ product_id: pid, attrs: edits[pid] || {} }))
      .filter((it) => Object.keys(it.attrs).length > 0);
    try {
      let saved = 0;
      if (bulkItems.length > 0) {
        const r1 = await pa.coupangSaveAttrsBulk(bulkItems);
        saved = r1.saved || 0;
      }
      const r2 = await pa.coupangRestorePending(selectedPids);
      setStatusMsg({ type: 'ok', text: `저장 ${saved}건 / pending 복구 ${r2.restored}건` });
      setSelected(new Set());
      setEdits({});
      refetch();
    } catch (e) {
      setStatusMsg({ type: 'error', text: e.message || '저장/복구 실패' });
    }
    setBusy(false);
  }, [selectedPids, edits, refetch]);

  const runRestoreOnly = useCallback(async () => {
    if (selectedPids.length === 0) return;
    setBusy(true);
    setStatusMsg(null);
    try {
      const r = await pa.coupangRestorePending(selectedPids);
      setStatusMsg({ type: 'ok', text: `pending 복구 ${r.restored}건` });
      setSelected(new Set());
      refetch();
    } catch (e) {
      setStatusMsg({ type: 'error', text: e.message || '복구 실패' });
    }
    setBusy(false);
  }, [selectedPids, refetch]);

  const attrEntries = useMemo(() => {
    return Object.entries(byAttr).sort((a, b) => b[1] - a[1]);
  }, [byAttr]);

  const total = data?.total || 0;

  return (
    <div className="space-y-4">
      {/* 재추출 진행바 */}
      {reextractProgress && (
        <Card padded>
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm">
              <span>
                {reextractProgress.status === 'done'
                  ? `AI 재추출 완료 — 채움 ${reextractProgress.processed}건, 실패 ${reextractProgress.errors}건`
                  : reextractProgress.status === 'error'
                    ? `오류: ${reextractProgress.message || '알 수 없는 오류'}`
                    : `AI 재추출 중 ${reextractProgress.processed + reextractProgress.errors}/${reextractProgress.total}`}
              </span>
              {reextractProgress.status !== 'running' && (
                <Button size="sm" variant="ghost" onClick={() => setReextractProgress(null)}>닫기</Button>
              )}
            </div>
            <div className="h-2 rounded-full bg-ink-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-purple-500 transition-all duration-300"
                style={{ width: `${reextractProgress.pct ?? 0}%` }}
              />
            </div>
          </div>
        </Card>
      )}

      {/* 상태 메시지 */}
      {statusMsg && (
        <Card padded>
          <div className="flex items-center justify-between text-sm">
            <span className={statusMsg.type === 'error' ? 'text-red-600' : 'text-green-700'}>
              {statusMsg.text}
            </span>
            <Button size="sm" variant="ghost" onClick={() => setStatusMsg(null)}>닫기</Button>
          </div>
        </Card>
      )}

      {/* 속성 필터 chips */}
      <Card padded>
        <div className="flex flex-wrap gap-2 text-xs">
          <button
            type="button"
            onClick={() => setAttrFilter('ALL')}
            className={`px-3 py-1.5 rounded-full border transition ${
              attrFilter === 'ALL'
                ? 'bg-pa-500 text-white border-pa-500'
                : 'bg-white text-ink-600 border-ink-200 hover:border-pa-300'
            }`}
          >
            전체 ({total})
          </button>
          {attrEntries.map(([name, count]) => (
            <button
              key={name}
              type="button"
              onClick={() => setAttrFilter(name)}
              className={`px-3 py-1.5 rounded-full border transition ${
                attrFilter === name
                  ? 'bg-pa-500 text-white border-pa-500'
                  : 'bg-white text-ink-600 border-ink-200 hover:border-pa-300'
              }`}
            >
              {name} ({count})
            </button>
          ))}
        </div>
      </Card>

      {/* 벌크 액션 바 */}
      <Card padded>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="text-sm text-ink-600">
            <span className="font-medium">{selected.size}</span>건 선택됨
            <span className="text-ink-400"> / 표시 중 {filteredItems.length}건</span>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={toggleAllVisible} disabled={busy}>
              {filteredItems.every((r) => selected.has(r.product_id)) && filteredItems.length > 0
                ? '전체 선택 해제'
                : '전체 선택'}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy || selected.size === 0}
              onClick={runRestoreOnly}
            >
              ↩️ pending 복구만
            </Button>
            <Button
              size="sm"
              variant="ds"
              disabled={busy || selected.size === 0}
              onClick={runSaveAndRestore}
            >
              ✍️ 수동 저장 + 복구
            </Button>
            <Button
              size="sm"
              variant="ds"
              disabled={busy || selected.size === 0 || reextractProgress?.status === 'running'}
              onClick={runReextract}
            >
              🤖 AI 엄격 재추출
            </Button>
          </div>
        </div>
      </Card>

      {/* 테이블 */}
      <Card padded={false}>
        {isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : !filteredItems.length ? (
          <div className="p-8 text-center text-sm text-ink-400">
            {attrFilter === 'ALL'
              ? 'MANDATORY 속성 부족으로 excluded 된 상품이 없습니다.'
              : `"${attrFilter}" 누락 상품이 없습니다.`}
          </div>
        ) : (
          <DataTable
            columns={[
              {
                key: '_sel', label: '', width: '40px',
                render: (_, row) => (
                  <input
                    type="checkbox"
                    checked={selected.has(row.product_id)}
                    onChange={() => toggleOne(row.product_id)}
                  />
                ),
              },
              { key: 'product_id', label: 'ID', width: '60px' },
              { key: 'title_ko', label: '상품명', wrap: true, maxWidth: '260px',
                render: (v, row) => v || row.title_en || '—' },
              { key: 'asin', label: 'ASIN', width: '110px' },
              { key: 'missing_attr', label: '누락 속성', width: '130px',
                render: (v) => <span className="text-xs font-medium text-red-600">{v}</span> },
              {
                key: '_edit', label: '값 입력', width: '180px',
                render: (_, row) => (
                  <input
                    type="text"
                    placeholder={row.saved_attrs?.[row.missing_attr] || '예: 60정'}
                    value={(edits[row.product_id] || {})[row.missing_attr] || ''}
                    onChange={(e) => updateEdit(row.product_id, row.missing_attr, e.target.value)}
                    className="w-full text-xs px-2 py-1 border border-ink-200 rounded focus:outline-none focus:border-pa-400"
                  />
                ),
              },
              {
                key: 'saved_attrs', label: '기존 저장', wrap: true, maxWidth: '200px',
                render: (v) => {
                  const keys = v ? Object.keys(v) : [];
                  if (!keys.length) return <span className="text-ink-300 text-xs">-</span>;
                  return (
                    <div className="text-xs space-y-0.5">
                      {keys.map((k) => (
                        <div key={k}>
                          <span className="text-ink-500">{k}</span>: <span className="text-ink-800">{v[k]}</span>
                        </div>
                      ))}
                    </div>
                  );
                },
              },
            ]}
            rows={filteredItems}
            rowKey={(r) => r.product_id}
          />
        )}
      </Card>
    </div>
  );
}
