import React, { useState, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, Legend,
} from 'recharts';
import { Card, KPICard, Button, LockChip, EmptyState } from '@charisg/ui';
import { pa } from '../api/pa.js';
import { useChannel } from '../channel.jsx';

const won = (v) => (v != null ? '₩' + Number(v).toLocaleString() : '—');
const wonShort = (v) => {
  if (v == null) return '—';
  const n = Number(v);
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(1) + '억';
  if (Math.abs(n) >= 1e4) return Math.round(n / 1e4).toLocaleString() + '만';
  return n.toLocaleString();
};
const PAID = '#16a34a';   // green-600 (지급완료)
const SCHED = '#f59e0b';  // amber-500 (지급예정)

function trendOf(delta) {
  if (delta == null) return 'flat';
  return delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat';
}

// ── 월별 지급 차트 (지급완료 + 지급예정 스택) ───────────────
function MonthlyChart({ months }) {
  if (!months?.length) return <div className="py-12 text-center text-sm text-ink-400">데이터 없음 — 동기화를 실행하세요.</div>;
  const data = months.map((m) => ({
    ym: m.ym,
    paid: m.final_paid || 0,
    scheduled: m.final_scheduled || 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={300}>
      <BarChart data={data} margin={{ top: 16, right: 16, left: 8, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#eef0f4" vertical={false} />
        <XAxis dataKey="ym" tick={{ fontSize: 12, fill: '#5b6376' }} axisLine={false} tickLine={false} />
        <YAxis tickFormatter={wonShort} tick={{ fontSize: 11, fill: '#9aa1b1' }} axisLine={false} tickLine={false} width={48} />
        <Tooltip
          formatter={(v, name) => [won(v), name === 'paid' ? '지급완료' : '지급예정']}
          labelFormatter={(l) => `${l}`}
          contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #eef0f4' }}
        />
        <Legend formatter={(v) => (v === 'paid' ? '지급완료' : '지급예정')} iconType="circle" wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="paid" stackId="a" fill={PAID} maxBarSize={56} />
        <Bar dataKey="scheduled" stackId="a" fill={SCHED} radius={[4, 4, 0, 0]} maxBarSize={56} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// 정산 유형 라벨 (채널별)
const TYPE_LABEL = {
  SALE: '판매', REFUND: '환불',
  PROD_ORDER: '상품', DELIVERY: '배송비', EXTRAFEE: '기타', DEDUCTION_RESTORE: '공제환급',
};
const isMinus = (t) => t === 'REFUND' || (t || '').includes('CANCEL');

// ── 주문/건별 상세 (월 필터 + keyset 더보기) ────────────────
function RevenueDetail({ months, channel, account }) {
  const [ym, setYm] = useState('');           // '' = 전체
  const [saleType, setSaleType] = useState(''); // '' = 전체
  const [rows, setRows] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const isNaver = channel === 'naver';

  const monthRange = (m) => (m ? { date_from: `${m}-01`, date_to: `${m}-31` } : {});

  const load = useCallback(async (reset) => {
    setLoading(true);
    try {
      const params = { channel, ...monthRange(ym), limit: 50 };
      if (saleType) params.sale_type = saleType;
      if (account) params.account = account;
      if (!reset && cursor) params.before_id = cursor;
      const res = await pa.settlementRevenue(params);
      // items 가 없는 응답이 한 번만 와도 아래 rows.map 에서 화면이 통째로 죽는다.
      const items = Array.isArray(res?.items) ? res.items : [];
      setRows((prev) => (reset ? items : [...prev, ...items]));
      setCursor(res?.next_cursor ?? null);
      setHasMore(!!res?.has_more);
    } finally {
      setLoading(false);
    }
  }, [ym, saleType, cursor, channel, account]);

  // 채널 변경 시 필터 초기화
  React.useEffect(() => { setYm(''); setSaleType(''); }, [channel]);
  // 필터 변경 시 첫 페이지 재조회
  React.useEffect(() => { setRows([]); setCursor(null); setHasMore(false); load(true); /* eslint-disable-next-line */ }, [ym, saleType, channel, account]);

  return (
    <Card
      title={isNaver ? '건별 정산 상세' : '주문별 정산 상세'}
      action={
        <div className="flex items-center gap-2">
          <select value={ym} onChange={(e) => setYm(e.target.value)}
            className="rounded-md border border-ink-200 px-2 py-1 text-xs text-ink-700">
            <option value="">전체 기간</option>
            {months.map((m) => <option key={m.ym} value={m.ym}>{m.ym}</option>)}
          </select>
          {!isNaver && (
            <select value={saleType} onChange={(e) => setSaleType(e.target.value)}
              className="rounded-md border border-ink-200 px-2 py-1 text-xs text-ink-700">
              <option value="">전체</option>
              <option value="SALE">판매</option>
              <option value="REFUND">환불</option>
            </select>
          )}
        </div>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-ink-100 text-left text-xs text-ink-500">
              <th className="py-2 pr-3 font-medium">주문번호</th>
              {isNaver && <th className="py-2 pr-3 font-medium">상품명</th>}
              <th className="py-2 pr-3 font-medium">유형</th>
              <th className="py-2 pr-3 font-medium">{isNaver ? '정산기준일' : '인식일'}</th>
              <th className="py-2 pr-3 font-medium">정산예정일</th>
              <th className="py-2 pr-3 text-right font-medium">{isNaver ? '결제정산' : '판매가'}</th>
              <th className="py-2 pr-3 text-right font-medium">수수료</th>
              <th className="py-2 pr-3 text-right font-medium">정산액</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b border-ink-50 text-ink-800">
                <td className="py-2 pr-3 font-mono text-xs">{r.order_id || '—'}</td>
                {isNaver && (
                  <td className="py-2 pr-3 max-w-[220px] truncate text-ink-600" title={r.product_name || ''}>
                    {r.product_name || '—'}
                  </td>
                )}
                <td className="py-2 pr-3">
                  <span className={isMinus(r.sale_type) ? 'text-signal-err' : 'text-ink-600'}>
                    {TYPE_LABEL[r.sale_type] || r.sale_type || '—'}
                  </span>
                </td>
                <td className="py-2 pr-3 text-ink-500">{r.recognition_date || '—'}</td>
                <td className="py-2 pr-3 text-ink-500">{r.settlement_date || '—'}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{won(r.sale_price)}</td>
                <td className="py-2 pr-3 text-right tabular-nums text-ink-500">{won(r.service_fee)}</td>
                <td className="py-2 pr-3 text-right font-medium tabular-nums">{won(r.settlement_amount)}</td>
              </tr>
            ))}
            {!rows.length && !loading && (
              <tr><td colSpan={isNaver ? 8 : 7} className="py-10 text-center text-ink-400">내역 없음</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex items-center justify-center">
        {hasMore ? (
          <Button variant="ghost" onClick={() => load(false)} disabled={loading}>
            {loading ? '불러오는 중…' : '더 보기'}
          </Button>
        ) : (
          rows.length > 0 && <span className="text-xs text-ink-400">마지막 내역</span>
        )}
      </div>
    </Card>
  );
}

// 정산 API 의 채널 코드는 콘솔의 채널 코드와 이름이 다르다(스마트스토어 → naver).
// 여기서만 변환한다 — 콘솔 전역 코드를 API 사정에 맞추면 다른 화면이 오염된다.
const SETTLEMENT_CHANNEL = { coupang: 'coupang', smartstore: 'naver' };

export default function SettlementPage() {
  const qc = useQueryClient();
  const [syncing, setSyncing] = useState(false);
  // 채널·계정 모두 상단이 정한다. 정산은 사업자 단위로 갈리는 돈이라
  // 화면 안에서 또 바꿀 수 있으면 어느 계정 정산을 보는지 헷갈릴 여지가 크다.
  const { channel: consoleChannel, account, channelMeta, accountMeta } = useChannel();
  const channel = SETTLEMENT_CHANNEL[consoleChannel] || null;
  const acctParam = consoleChannel === 'all' ? undefined : account;

  const { data: summary, isLoading } = useQuery({
    queryKey: ['pa', 'settlement', 'summary', channel, acctParam ?? 'all'],
    queryFn: () => pa.settlementSummary({ channel, account: acctParam }),
    enabled: !!channel,
  });
  const { data: status } = useQuery({
    queryKey: ['pa', 'settlement', 'status', channel],
    queryFn: () => pa.settlementStatus(channel),
    refetchInterval: (q) => (q?.state?.data?.running ? 4000 : false),
    enabled: !!channel,
  });

  const months = summary?.months || [];
  const last = months[months.length - 1];

  const onSync = async () => {
    setSyncing(true);
    try {
      await pa.syncSettlement({ channel });
      // 진행상태 폴링 시작 → 완료되면 요약 갱신
      const poll = setInterval(async () => {
        const s = await qc.fetchQuery({ queryKey: ['pa', 'settlement', 'status', channel], queryFn: () => pa.settlementStatus(channel) });
        if (!s.running) {
          clearInterval(poll);
          qc.invalidateQueries({ queryKey: ['pa', 'settlement'] });
          setSyncing(false);
        }
      }, 4000);
    } catch {
      setSyncing(false);
    }
  };

  const running = syncing || status?.running;

  // 정산 데이터가 있는 채널은 쿠팡·스마트스토어뿐이다. 전체 탭이나 11번가·ESM 에서는
  // 빈 표를 보여주는 대신 무엇을 해야 하는지 말한다.
  if (!channel) {
    return (
      <Card>
        <EmptyState
          title="정산은 채널 단위로 봅니다"
          description={
            consoleChannel === 'all'
              ? '상단 탭에서 쿠팡 또는 스마트스토어를 고르면 그 계정의 정산이 열립니다.'
              : `${channelMeta.label}은(는) 아직 정산 연동이 없습니다.`
          }
        />
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">정산</h1>
          <p className="mt-1 text-sm text-ink-500">
            월별 지급액과 전월 대비 증감, {channel === 'naver' ? '건별' : '주문별'} 정산 내역 (2026-01~)
            {status?.revenue_last_synced && (
              <span className="ml-2 text-ink-400">· 최근 동기화 {status.revenue_last_synced}</span>
            )}
          </p>
        </div>
        <Button onClick={onSync} disabled={running} variant="pa">
          {running ? '동기화 중…' : '정산 동기화'}
        </Button>
      </div>

      {/* 범위는 상단 채널 탭·계정 줄이 정한다 */}
      <div className="flex items-center gap-2">
        <LockChip title="상단 채널 탭에서 변경">{channelMeta.label}</LockChip>
        {accountMeta && <LockChip title="상단 계정 줄에서 변경">{accountMeta.label}</LockChip>}
      </div>

      {/* KPI */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {summary?.realtime_order_revenue != null && (
          <KPICard
            label="실시간 주문 매출"
            value={won(summary?.realtime_order_revenue)}
            accent="pa"
            hint={`주문 ${summary?.realtime_order_count || 0}건 · 정산前 포함(쿠팡 누적매출 대조)`}
          />
        )}
        <KPICard
          label="누적 지급완료"
          value={won(summary?.total_paid)}
          accent="pa"
          hint="실입금 완료"
        />
        <KPICard
          label="누적 지급예정"
          value={won(summary?.total_scheduled)}
          accent="pa"
          hint="아직 미입금"
        />
        <KPICard
          label={last ? `${last.ym} 지급액(합)` : '최근월 지급액'}
          value={won(last?.final_amount)}
          delta={last?.mom_pct != null ? Math.abs(last.mom_pct) : undefined}
          trend={trendOf(last?.mom_delta)}
          accent="pa"
          hint={last?.mom_delta != null ? `전월대비 ${last.mom_delta >= 0 ? '+' : ''}${won(last.mom_delta)}` : '완료+예정'}
        />
      </div>

      {/* 월별 차트 */}
      <Card title="월별 지급액 (지급완료 + 지급예정 스택)">
        {isLoading ? <div className="py-12 text-center text-ink-400">로딩 중…</div> : <MonthlyChart months={months} />}
      </Card>

      {/* 월별 요약 테이블 */}
      <Card title="월별 정산 요약">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-ink-100 text-left text-xs text-ink-500">
                <th className="py-2 pr-3 font-medium">월</th>
                <th className="py-2 pr-3 text-right font-medium">매출</th>
                <th className="py-2 pr-3 text-right font-medium">수수료</th>
                <th className="py-2 pr-3 text-right font-medium">지급완료</th>
                <th className="py-2 pr-3 text-right font-medium">지급예정</th>
                <th className="py-2 pr-3 text-right font-medium">전월대비</th>
              </tr>
            </thead>
            <tbody>
              {months.map((m) => {
                const t = trendOf(m.mom_delta);
                return (
                  <tr key={m.ym} className="border-b border-ink-50 text-ink-800">
                    <td className="py-2 pr-3 font-medium">{m.ym}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-ink-500">{won(m.total_sale)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums text-ink-500">{won(m.service_fee)}</td>
                    <td className="py-2 pr-3 text-right font-semibold tabular-nums text-signal-ok">{won(m.final_paid)}</td>
                    <td className="py-2 pr-3 text-right tabular-nums" style={{ color: m.final_scheduled ? SCHED : undefined }}>
                      {m.final_scheduled ? won(m.final_scheduled) : '—'}
                    </td>
                    <td className="py-2 pr-3 text-right tabular-nums">
                      {m.mom_delta == null ? (
                        <span className="text-ink-300">기준</span>
                      ) : (
                        <span className={t === 'up' ? 'text-signal-ok' : t === 'down' ? 'text-signal-err' : 'text-ink-400'}>
                          {t === 'up' ? '▲' : t === 'down' ? '▼' : ''} {m.mom_pct != null ? `${Math.abs(m.mom_pct)}%` : ''}
                          <span className="ml-1 text-xs text-ink-400">({m.mom_delta >= 0 ? '+' : ''}{wonShort(m.mom_delta)})</span>
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {!months.length && (
                <tr><td colSpan={6} className="py-10 text-center text-ink-400">데이터 없음 — 정산 동기화를 실행하세요.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 주문별/건별 상세 */}
      <RevenueDetail months={months} channel={channel} account={acctParam} />
    </div>
  );
}
