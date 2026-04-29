import React, { useState, useMemo } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge, DataTable } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function GroupDetailPage() {
  const { parentAsin } = useParams();
  const [params, setParams] = useSearchParams();
  const channel = params.get('channel') || 'coupang';
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'group', parentAsin, channel],
    queryFn: () => pa.groupGet(parentAsin, channel),
  });

  const [overrideDim, setOverrideDim] = useState(null);
  const [savingRule, setSavingRule] = useState(false);
  const [savedMsg, setSavedMsg] = useState(null);
  const [previewKey, setPreviewKey] = useState(null);
  const [extendDryRun, setExtendDryRun] = useState(null);
  const [extendBusy, setExtendBusy] = useState(false);
  const [extendResult, setExtendResult] = useState(null);
  const [extendError, setExtendError] = useState(null);

  const previewQuery = useQuery({
    queryKey: ['pa', 'group-payload', parentAsin, previewKey],
    queryFn: () => {
      const [ch, idx] = previewKey.split(':');
      return pa.groupPayload(parentAsin, ch, parseInt(idx, 10));
    },
    enabled: !!previewKey,
    retry: false,
  });

  if (isLoading) return <div className="p-8 text-sm text-ink-400">로딩 중...</div>;
  if (!data) return <div className="p-8 text-sm text-red-600">그룹을 찾을 수 없음</div>;

  const switchChannel = (c) => {
    params.set('channel', c);
    setParams(params);
  };

  const saveRuleAndRefetch = async (newDim) => {
    if (!data.category_path) {
      alert('카테고리 정보 없음 — 학습 룰 저장 불가. 먼저 master 의 category_path 확인.');
      return;
    }
    setSavingRule(true);
    setSavedMsg(null);
    try {
      await pa.groupSaveRule(parentAsin, {
        category_path: data.category_path,
        dim_priority: [newDim],
      });
      setOverrideDim(newDim);
      setSavedMsg(`✓ ${data.category_path} → ${newDim} 룰 저장 — 다음 같은 카테고리 그룹에 자동 적용됩니다.`);
      qc.invalidateQueries({ queryKey: ['pa', 'group', parentAsin] });
    } catch (e) {
      setSavedMsg(`✗ 저장 실패: ${e.message || ''}`);
    } finally {
      setSavingRule(false);
    }
  };

  const limit = data.channel_limit || 30;
  const overLimit = data.child_count > limit;

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <Link to="/groups" className="text-xs text-ink-500 hover:text-pa-600">← 옵션 그룹 목록</Link>
          <h1 className="mt-1 text-xl font-semibold tracking-tight text-ink-900">
            {data.base_name_ko || data.base_name_en || data.parent_asin}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-ink-500">
            <span className="font-mono">parent: {data.parent_asin}</span>
            <span>brand: {data.brand || '—'}</span>
            <span>theme: <span className="font-mono">{data.variation_theme || '—'}</span></span>
            <span>옵션: <strong className={overLimit ? 'text-amber-700' : 'text-ink-900'}>{data.child_count}</strong></span>
            <a
              href={`https://www.amazon.com/dp/${data.parent_asin}`}
              target="_blank" rel="noopener noreferrer"
              className="text-pa-600 hover:underline"
            >Amazon 열기 ↗</a>
          </div>
        </div>

        {/* 채널 토글 + 통합 등록 */}
        <div className="flex flex-col gap-2 items-end">
          <div className="flex gap-1">
            {['coupang', 'smartstore'].map((c) => (
              <button
                key={c}
                onClick={() => switchChannel(c)}
                className={`px-3 py-1.5 rounded-md text-xs border transition ${
                  channel === c
                    ? 'bg-pa-500 text-white border-pa-500'
                    : 'bg-white text-ink-600 border-ink-200 hover:border-pa-300'
                }`}
              >
                {c}
              </button>
            ))}
          </div>
          <Button
            size="sm"
            variant="ds"
            disabled={extendBusy}
            onClick={async () => {
              setExtendBusy(true); setExtendError(null); setExtendResult(null);
              try {
                const res = await pa.groupExtend(parentAsin, { dry_run: true });
                setExtendDryRun(res);
              } catch (e) {
                setExtendError(e.message || '분석 실패');
              } finally {
                setExtendBusy(false);
              }
            }}
          >
            🔍 옵션 통합 분석
          </Button>
        </div>
      </header>

      {/* 분리 차원 */}
      <Card title="분리 차원 결정" padded>
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>현재 채널({channel}, 한도 {limit}):</span>
            {data.primary_dim ? (
              <>
                <StatusBadge variant="ok">{data.primary_dim}</StatusBadge>
                <span className="text-xs text-ink-500">
                  결정 근거: <span className="font-mono">{data.primary_dim_source}</span>
                </span>
              </>
            ) : (
              <StatusBadge variant="warn">분리 불가 (top N 자르기)</StatusBadge>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-ink-500">차원 변경:</span>
            {(data.variation_dimensions || []).map((dim) => (
              <button
                key={dim}
                onClick={() => saveRuleAndRefetch(dim)}
                disabled={savingRule}
                className={`px-3 py-1.5 rounded-md text-xs border transition ${
                  (overrideDim || data.primary_dim) === dim
                    ? 'bg-ink-700 text-white border-ink-700'
                    : 'bg-white text-ink-600 border-ink-200 hover:border-pa-300'
                } disabled:opacity-50`}
              >
                {dim}
              </button>
            ))}
            {savingRule && <span className="text-xs text-ink-400">저장 중...</span>}
          </div>

          {savedMsg && (
            <div className="text-xs text-ink-600 bg-ink-50 p-2 rounded">{savedMsg}</div>
          )}
          {data.category_path && (
            <div className="text-[11px] text-ink-400">
              카테고리: <span className="font-mono">{data.category_path}</span>
            </div>
          )}
        </div>
      </Card>

      {/* 분리 미리보기 */}
      <Card title={`${channel} 분리 미리보기 (${data.splits.length} listing)`} padded={false}>
        <DataTable
          columns={[
            { key: '_preview', label: '', width: '90px',
              render: (_, row) => (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setPreviewKey(`${channel}:${row.id}`)}
                >
                  페이로드 ↗
                </Button>
              ) },
            { key: 'name', label: '리스팅 상품명', wrap: true, maxWidth: '420px' },
            { key: 'split_dim', label: '분리 차원', width: '110px',
              render: (v) => v ? <span className="font-mono text-xs">{v}</span> : '—' },
            { key: 'split_value_korean', label: '값', width: '160px',
              render: (v) => v || '—' },
            { key: 'size', label: '옵션 수', width: '90px',
              render: (v) => <strong>{v}</strong> },
            { key: 'skipped_count', label: '잘림', width: '70px',
              render: (v) => v ? <span className="text-xs text-amber-700">−{v}</span> : '—' },
            { key: 'split_source', label: '근거', width: '110px',
              render: (v) => <span className="text-[11px] font-mono text-ink-500">{v || '—'}</span> },
          ]}
          rows={(data.splits || []).map((s, i) => ({ id: i, ...s }))}
          rowKey={(r) => r.id}
          pageSize={20}
        />
      </Card>

      {/* Children 표 */}
      <Card title={`Children (${data.children.length})`} padded={false}>
        <DataTable
          columns={[
            { key: 'asin', label: 'ASIN', width: '110px',
              render: (v) => (
                <a href={`https://www.amazon.com/dp/${v}`} target="_blank" rel="noopener noreferrer"
                   className="font-mono text-xs text-pa-600 hover:underline">
                  {v} ↗
                </a>
              ) },
            { key: 'image_url', label: '이미지', width: '60px',
              render: (v) => v
                ? <img src={v} alt="" className="w-10 h-10 object-cover rounded" />
                : <div className="w-10 h-10 rounded bg-ink-100" /> },
            { key: 'size_label', label: 'size', width: '110px',
              render: (v) => v ? <span className="text-xs">{v}</span> : '—' },
            { key: 'color', label: 'color', width: '90px',
              render: (v) => v || '—' },
            { key: 'flavor_attr', label: 'flavor', width: '110px',
              render: (v) => v || '—' },
            { key: 'item_weight_g', label: '무게(g)', width: '80px',
              render: (v) => v ? Math.round(v) : '—' },
            { key: 'cost_usd', label: 'cost USD', width: '90px',
              render: (v) => v ? `$${v.toFixed(2)}` : '—' },
            { key: 'sale_krw', label: '판매가', width: '110px',
              render: (v) => v ? '₩' + Number(v).toLocaleString() : '—' },
            { key: 'sales_rank', label: 'BSR', width: '90px',
              render: (v) => v ? v.toLocaleString() : '—' },
          ]}
          rows={(data.children || []).map((c, i) => ({ id: i, ...c }))}
          rowKey={(r) => r.id}
          pageSize={50}
        />
      </Card>

      {/* 옵션 통합 분석 + 실등록 */}
      {(extendDryRun || extendError) && (
        <Card title="🎁 옵션 통합 등록" padded>
          {extendError && <div className="text-sm text-red-600">{extendError}</div>}
          {extendDryRun && (
            <div className="space-y-3">
              {/* mode 설명 라벨 */}
              <div className={`rounded-md border p-3 text-xs ${
                extendDryRun.mode === 'register'
                  ? 'bg-blue-50 border-blue-200 text-blue-900'
                  : 'bg-purple-50 border-purple-200 text-purple-900'
              }`}>
                <strong>모드: {extendDryRun.mode}</strong>
                {extendDryRun.mode === 'extend' ? (
                  <div className="mt-1">
                    이 그룹은 이미 채널에 등록된 master listing 이 있습니다.
                    master 의 originProduct/sellerProduct 에 옵션만 추가하고,
                    같은 그룹의 다른 단일 listing 들은 SUSPEND/STOP_SALES 됩니다.
                    <br/>(검색·리뷰 master 1건은 보존)
                  </div>
                ) : (
                  <div className="mt-1">
                    이 그룹은 master listing 이 없어 신규 multi-option 으로 채널 등록합니다.
                    같은 그룹의 기존 단일 listing 이 있다면 모두 archive 됩니다.
                    <br/>옵션별 channel_option_id 자동 매핑 → 주문 추적 자동 작동.
                  </div>
                )}
              </div>

              {/* 영향 요약 — extend / register 별 */}
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {['smartstore', 'coupang'].map((ch) => {
                  const ci = extendDryRun.channels?.[ch];
                  if (!ci) return null;
                  // extend mode 결과 (단일 객체) vs register mode 결과 (배열)
                  const isRegister = Array.isArray(ci);
                  const items = isRegister ? ci : [ci];
                  return (
                    <div key={ch} className="rounded-md border bg-ink-50 border-ink-200 p-3">
                      <div className="flex items-center justify-between mb-2">
                        <strong className="text-sm">{ch}</strong>
                        <span className="text-[11px] text-ink-500">{isRegister ? `${items.length} split` : ''}</span>
                      </div>
                      {items.map((it, idx) => {
                        const ok = it.action === 'dry_run' || it.status === 'dry_run';
                        return (
                          <div key={idx} className={`mt-1 text-xs space-y-0.5 ${ok ? '' : 'text-red-700'}`}>
                            {ok ? (
                              <>
                                {it.master_channel_product_id && (
                                  <div>master listing: <code className="font-mono">{it.master_channel_product_id}</code></div>
                                )}
                                {it.master_child_asin && (
                                  <div>master ASIN: <code className="font-mono">{it.master_child_asin}</code></div>
                                )}
                                {it.split_name && (
                                  <div>리스팅: {it.split_name.slice(0, 60)}</div>
                                )}
                                <div>옵션 수: <strong>{it.options_in_payload || it.options_count}</strong></div>
                                {it.subordinate_count !== undefined && (
                                  <div>archive 대상: <strong className="text-amber-700">{it.subordinate_count}건</strong></div>
                                )}
                                {it.new_options_count !== undefined && (
                                  <div>신규 옵션: {it.new_options_count}건</div>
                                )}
                              </>
                            ) : (
                              <div>{it.reason || it.error || it.action || it.status}</div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  );
                })}
              </div>

              {/* 영향 경고 + 실행 */}
              <div className="rounded-md bg-amber-50 border border-amber-200 p-3 text-xs text-amber-900">
                ⚠️ 실등록 시 master 외 listing 의 채널 등록이 SUSPEND/STOP_SALES 됩니다 (검색 reset 가능).
                <br/>쿠팡은 옵션 추가 후 <strong>재승인 검수 1~3일</strong> 소요.
                <br/>옵션 매핑 실패 시 listing_options.channel_option_id 가 NULL — backfill 잡으로 추후 보완.
              </div>

              <div className="flex justify-between items-center">
                <Button size="sm" variant="ghost" onClick={() => setExtendDryRun(null)}>닫기</Button>
                <Button
                  size="sm"
                  variant="ds"
                  disabled={extendBusy}
                  onClick={async () => {
                    if (!confirm('정말 통합 등록을 진행합니다. 채널에 즉시 반영되며 일부 listing 이 archive 됩니다. 계속?')) return;
                    setExtendBusy(true); setExtendError(null);
                    try {
                      const res = await pa.groupExtend(parentAsin, { dry_run: false, confirm: true });
                      setExtendResult(res);
                      setExtendDryRun(null);
                    } catch (e) {
                      setExtendError(e.message || '실등록 실패');
                    } finally {
                      setExtendBusy(false);
                    }
                  }}
                >
                  ⚡ 통합 등록 진행 (실 채널)
                </Button>
              </div>
            </div>
          )}
        </Card>
      )}

      {extendResult && (
        <Card title="✅ 옵션 통합 등록 결과" padded>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 text-xs">
            {['smartstore', 'coupang'].map((ch) => {
              const ci = extendResult.channels?.[ch];
              if (!ci) return null;
              const ok = ci.action === 'extended';
              return (
                <div key={ch} className={`rounded-md border p-3 ${ok ? 'bg-green-50 border-green-300' : 'bg-red-50 border-red-300'}`}>
                  <div className="flex items-center justify-between">
                    <strong>{ch}</strong>
                    <span>{ci.action}</span>
                  </div>
                  {ok && (
                    <div className="mt-2">
                      <div>extend OK ✓</div>
                      <div>archive: {ci.subordinates_suspended || ci.subordinates_stopped}/{ci.subordinates_total}건</div>
                      {ci.needs_reapproval && <div className="text-amber-700">⚠ 쿠팡 재승인 검수 1~3일 대기</div>}
                    </div>
                  )}
                  {!ok && <div className="mt-2 text-red-700">{ci.reason || ci.stage}</div>}
                </div>
              );
            })}
          </div>
          <div className="mt-3 flex justify-end">
            <Button size="sm" variant="ghost" onClick={() => setExtendResult(null)}>닫기</Button>
          </div>
        </Card>
      )}

      {/* 페이로드 미리보기 (선택된 split) */}
      {previewKey && (
        <Card
          title={`📋 dry-run 페이로드 미리보기 — ${previewKey.split(':')[0]} (split #${previewKey.split(':')[1]})`}
          padded
        >
          {previewQuery.isLoading ? (
            <div className="text-sm text-ink-400">로딩 중...</div>
          ) : previewQuery.error ? (
            <div className="text-sm text-red-600">조회 실패: {previewQuery.error.message}</div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-sm">
                <div className="text-ink-700">
                  <strong>{previewQuery.data?.split_name}</strong>
                  <span className="ml-2 text-xs text-ink-500">옵션 {previewQuery.data?.options_count}개</span>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => {
                      navigator.clipboard.writeText(JSON.stringify(previewQuery.data?.payload, null, 2));
                    }}
                  >
                    📋 JSON 복사
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setPreviewKey(null)}>
                    닫기 ✕
                  </Button>
                </div>
              </div>
              <pre className="max-h-[600px] overflow-auto rounded-md bg-ink-900 p-4 text-xs leading-relaxed text-ink-100 font-mono">
                {JSON.stringify(previewQuery.data?.payload, null, 2)}
              </pre>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
