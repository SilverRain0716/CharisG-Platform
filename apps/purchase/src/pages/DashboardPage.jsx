import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card, KPICard, FunnelChart, Button } from '@charisg/ui';
import { pa } from '../api/pa.js';

import { useChannel } from '../channel.jsx';

const PROMPT_URL = '/purchase/prompts/amazon_kr_sourcing_v3.2.md';

/** 'channel:account' 카운트를 '구 157 · 신 19' 한 줄로. 계정이 하나뿐이면 감춘다. */
function acctHint(byAccount, channel) {
  if (!byAccount) return null;
  const old = byAccount[`${channel}:old`] || 0;
  const neo = byAccount[`${channel}:new`] || 0;
  if (!old && !neo) return null;
  return `구 ${old.toLocaleString()} · 신 ${neo.toLocaleString()}`;
}

function PromptCard() {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(null);

  const handleCopy = async () => {
    setError(null);
    try {
      const res = await fetch(PROMPT_URL);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      setError(e.message || '알 수 없는 오류');
      setTimeout(() => setError(null), 3000);
    }
  };

  return (
    <Card title="Amazon 소싱 프롬프트 (v3.2)">
      <p className="text-sm text-ink-600 mb-4">
        Claude.ai 웹 프로젝트 시스템 프롬프트에 붙여넣으면 디스커버리 키워드를 ASIN 11컬럼(이미지 URL 포함) 구글시트로 변환합니다.
      </p>
      <div className="flex items-center gap-2">
        <a
          href={PROMPT_URL}
          download="amazon_kr_sourcing_v3.2.md"
          className="inline-flex items-center rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent"
        >
          다운로드
        </a>
        <Button variant="ghost" size="sm" onClick={handleCopy}>
          {copied ? '복사됨 ✓' : '클립보드 복사'}
        </Button>
      </div>
      {error && (
        <div className="mt-3 text-sm text-signal-err">
          복사 실패: {error} (HTTPS 환경에서만 동작합니다)
        </div>
      )}
    </Card>
  );
}

function ChannelProductCounts() {
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(null);  // 'smartstore' | 'coupang' | null

  const ss = useQuery({
    queryKey: ['pa', 'smartstore', 'product-count'],
    queryFn: () => pa.smartstoreProductCount(false),
    staleTime: 60_000,
  });
  const cp = useQuery({
    queryKey: ['pa', 'coupang', 'product-count'],
    queryFn: () => pa.coupangProductCount(false),
    staleTime: 60_000,
  });

  const handleRefresh = async (channel) => {
    setRefreshing(channel);
    try {
      if (channel === 'smartstore') {
        await pa.smartstoreProductCount(true);
        qc.invalidateQueries({ queryKey: ['pa', 'smartstore', 'product-count'] });
      } else {
        await pa.coupangProductCount(true);
        qc.invalidateQueries({ queryKey: ['pa', 'coupang', 'product-count'] });
      }
    } finally {
      setRefreshing(null);
    }
  };

  const fmtAge = (sec) => {
    if (sec == null) return '';
    if (sec < 60) return `${sec}초 전`;
    if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
    return `${Math.floor(sec / 3600)}시간 전`;
  };

  return (
    <Card title="채널 등록 현황 (실 API 조회)">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {/* 스마트스토어 */}
        <div className="rounded-lg border border-ink-200 p-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ink-700">스마트스토어 (SALE)</h3>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleRefresh('smartstore')}
              disabled={refreshing === 'smartstore'}
            >
              {refreshing === 'smartstore' ? '조회 중...' : '새로고침'}
            </Button>
          </div>
          <div className="mt-2 text-3xl font-bold text-accent">
            {ss.isLoading ? '...' : (ss.data?.total ?? '-').toLocaleString()}
          </div>
          {/* ★한도는 계정마다 따로 걸린다. 합계만 보면 어느 쪽이 찼는지 알 수 없어
              신규 등록이 왜 막히는지 진단이 안 된다. */}
          <div className="mt-2 space-y-1.5">
            {[
              { k: 'old', label: '카리스G (구)' },
              { k: 'new', label: '카리스 글로벌 (신)' },
            ].map(({ k, label }) => {
              const n = ss.data?.by_account?.[k];
              const cap = ss.data?.limit_per_account || 1000;
              const pct = n == null ? 0 : Math.min(100, (n / cap) * 100);
              return (
                <div key={k}>
                  <div className="flex justify-between text-xs">
                    <span className="text-ink-600">{label}</span>
                    <span className={n != null && n >= cap ? 'font-medium text-signal-err' : 'font-medium text-ink-700'}>
                      {n == null
                        ? (ss.isLoading ? '…' : '조회 실패')
                        : `${n.toLocaleString()} / ${cap.toLocaleString()}`}
                    </span>
                  </div>
                  <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-ink-100">
                    <div
                      className={`h-full rounded-full ${pct >= 100 ? 'bg-signal-err' : pct >= 85 ? 'bg-signal-warn' : 'bg-accent'}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-2 text-xs text-ink-500">
            계정당 1,000 한도 · {ss.data?.cached ? `캐시 ${fmtAge(ss.data?.age_sec)}` : '방금 조회'}
          </div>
          {ss.error && <div className="mt-2 text-xs text-signal-err">조회 실패</div>}
        </div>

        {/* 쿠팡 */}
        <div className="rounded-lg border border-ink-200 p-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ink-700">쿠팡 (전체)</h3>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => handleRefresh('coupang')}
              disabled={refreshing === 'coupang'}
            >
              {refreshing === 'coupang' ? '조회 중 (~20초)...' : '새로고침'}
            </Button>
          </div>
          <div className="mt-2 text-3xl font-bold text-accent">
            {cp.isLoading ? '...' : (cp.data?.total ?? '-').toLocaleString()}
          </div>
          <div className="mt-1 text-xs text-ink-500">
            {cp.data?.cached ? `캐시 ${fmtAge(cp.data?.age_sec)}` : '방금 조회'}
          </div>
          {cp.data?.by_status && Object.keys(cp.data.by_status).length > 0 && (
            <div className="mt-3 space-y-1">
              {Object.entries(cp.data.by_status)
                .sort((a, b) => b[1] - a[1])
                .map(([status, count]) => (
                  <div key={status} className="flex justify-between text-xs">
                    <span className="text-ink-600">{status}</span>
                    <span className="font-mono text-ink-800">{count.toLocaleString()}</span>
                  </div>
                ))}
            </div>
          )}
          {cp.error && <div className="mt-2 text-xs text-signal-err">조회 실패</div>}
        </div>
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const { channel, account } = useChannel();
  const { data, isLoading } = useQuery({
    // ★queryKey 에 스코프를 넣어야 탭을 바꿀 때 다시 받아온다.
    queryKey: ['pa', 'dashboard', channel, account],
    queryFn: () => pa.dashboard({ channel, account }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">대시보드</h1>
        <p className="mt-1 text-sm text-ink-500">미국 아마존 → 한국 구매대행 파이프라인 조감.</p>
      </header>

      <PromptCard />

      {isLoading && <div className="text-sm text-ink-400">로딩 중...</div>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            {/* ★채널이 판매중이라 답한 것만 센다(2026-08-15 재정의).
                종전엔 products.status 를 셌는데 112건 중 실제 판매중은 1건이었다. */}
            <KPICard label="판매중 상품" value={data.kpis?.active_products?.toLocaleString() || 0} accent="pa" />
            <KPICard label="평균 마진"   value={`${data.kpis?.avg_margin || 0}%`} accent="pa" />
            <KPICard label="GO 대기"     value={data.todos?.go_pending || 0} accent="pa" />
            <KPICard label="업로드 대기" value={data.todos?.upload_pending || 0} accent="pa" />
            <KPICard label="미처리 CS"   value={data.todos?.cs_open || 0} accent="pa" />
          </div>

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <KPICard label="오늘 주문"     value={data.kpis?.orders_today || 0} accent="pa" />
            <KPICard label="처리 대기 주문" value={data.todos?.orders_pending || 0} accent="pa" />
            <KPICard
              label="쿠팡 누적"
              value={data.kpis?.orders_by_channel?.coupang || 0}
              hint={acctHint(data.kpis?.orders_by_account, 'coupang')}
              accent="pa"
            />
            <KPICard
              label="스마트스토어 누적"
              value={data.kpis?.orders_by_channel?.smartstore || 0}
              hint={acctHint(data.kpis?.orders_by_account, 'smartstore')}
              accent="pa"
            />
          </div>

          {((data.todos?.cancel_in_progress || 0) > 0 || (data.todos?.amazon_purchase_no_id || 0) > 0) && (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {(data.todos?.cancel_in_progress || 0) > 0 && (
                <div className="rounded-xl border border-signal-err/40 bg-soft-err p-4">
                  <div className="text-xs font-medium text-signal-err">⚠️ 취소 요청 진행 중</div>
                  <div className="mt-1 text-2xl font-semibold text-signal-err">
                    {data.todos.cancel_in_progress}건
                  </div>
                  <div className="mt-1 text-xs text-signal-err">
                    채널에 취소/출고중지 요청이 들어왔지만 아직 처리되지 않은 주문. 셀러센터에서 응대 필요.
                  </div>
                </div>
              )}
              {(data.todos?.amazon_purchase_no_id || 0) > 0 && (
                <div className="rounded-xl border border-signal-warn/40 bg-soft-warn p-4">
                  <div className="text-xs font-medium text-signal-warn">발주번호 누락</div>
                  <div className="mt-1 text-2xl font-semibold text-signal-warn">
                    {data.todos.amazon_purchase_no_id}건
                  </div>
                  <div className="mt-1 text-xs text-signal-warn">
                    아마존 구매 단계인데 amazon_order_id 가 비어있음. 주문 상세에서 발주번호 + 배송방식 입력 필요.
                  </div>
                </div>
              )}
            </div>
          )}

          <ChannelProductCounts />

          <Card title="파이프라인 퍼널">
            <FunnelChart
              stages={(data.funnel || []).map((s) => ({ ...s, color: 'bg-accent' }))}
            />
          </Card>
        </>
      )}
    </div>
  );
}
