import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  Card, Button, Segmented, TriageQueue, MetricStrip, Metric,
  ChannelMark, StatusBadge, EmptyState,
} from '@charisg/ui';
import { pa } from '../api/pa.js';
import { useChannel } from '../channel.jsx';

/**
 * 콘솔 첫 화면.
 *
 *   전체 탭   → 채널 × 계정 매트릭스 (어디가 살아있고 어디가 정리 대상인가)
 *   채널 탭   → 그 채널·계정의 처리 대기 + 지표
 *
 * 두 화면 다 위계가 같다: ①처리 대기 → ②지표 → ③상세.
 * 액션이 필요한 것과 참고 지표를 섞지 않는다.
 */
export default function ConsoleHome() {
  const { channel } = useChannel();
  return channel === 'all' ? <OverviewDashboard /> : <ChannelDashboard />;
}

/* ─────────────────────────────────────────────────────────
   전체 — 채널 × 계정 매트릭스
   ───────────────────────────────────────────────────────── */
function OverviewDashboard() {
  const navigate = useNavigate();
  const { account, channel, channels, withScope } = useChannel();
  const { data, isLoading } = useQuery({
    // ★queryKey 에 스코프를 넣어야 탭을 바꿀 때 다시 받아온다.
    queryKey: ['pa', 'dashboard', channel, account],
    queryFn: () => pa.dashboard({ channel, account }),
  });

  const ordersByAccount = data?.kpis?.orders_by_account || {};
  const totalOrders = Object.values(ordersByAccount).reduce((a, b) => a + b, 0);

  const usable = channels.flatMap((c) => c.accounts).filter((a) => a.usable);
  const active = usable.filter((a) => a.status === 'active');
  // 아직 안 쓰고 있는 등록 슬롯 — 놀고 있는 채널을 드러낸다
  const freeSlots = channels
    .flatMap((c) => c.accounts)
    .filter((a) => a.usable && a.limit_products)
    .reduce((sum, a) => sum + a.limit_products, 0);

  const todos = data?.todos || {};
  const items = [
    todos.cancel_in_progress && {
      id: 'cancel', severity: 'crit', label: '취소 요청 진행 중',
      desc: '채널에 취소·출고중지 요청이 접수됨 · 셀러센터 응대 필요',
      count: todos.cancel_in_progress, action: '주문', href: null,
      onClick: () => navigate(withScope('/orders')),
    },
    todos.amazon_purchase_no_id && {
      id: 'nopo', severity: 'crit', label: '발주번호 누락',
      desc: '아마존 구매 단계인데 amazon_order_id 가 비어 있음',
      count: todos.amazon_purchase_no_id, action: '주문',
      onClick: () => navigate(withScope('/orders')),
    },
    todos.orders_pending && {
      id: 'pend', severity: 'warn', label: '처리 대기 주문',
      count: todos.orders_pending, action: '주문',
      onClick: () => navigate(withScope('/orders')),
    },
    todos.go_pending && {
      id: 'go', severity: 'warn', label: 'GO 판정 대기', tag: '공통',
      desc: '스코어링 완료, 사람 확인만 남음',
      count: todos.go_pending, action: '소싱',
      onClick: () => navigate(withScope('/sourcing')),
    },
    todos.cs_open && {
      id: 'cs', severity: 'warn', label: '미처리 CS',
      count: todos.cs_open, action: 'CS·반품',
      onClick: () => navigate(withScope('/cs-returns')),
    },
  ].filter(Boolean);

  return (
    <div className="grid gap-3">
      <PageHead
        title="대시보드"
        badge={<span className="inline-flex items-center gap-1.5"><ChannelMark channel="all" mark="ALL" /> 전체</span>}
        sub="모든 채널·계정 합산"
      />

      <TriageQueue
        items={items}
        total={items.reduce((s, i) => s + (Number(i.count) || 0), 0)}
        hint="누르면 해당 화면이 그 조건으로 열립니다"
      />

      <MetricStrip cols={5}>
        {/* ★채널이 판매중이라 답한 상품 수. 우리 장부가 아니라 채널이 진실이다. */}
        <Metric label="판매중 상품" value={fmt(data?.kpis?.active_products)} />
        <Metric label="누적 주문" value={fmt(totalOrders)} hint={isLoading ? '' : '전 계정'} />
        <Metric label="평균 마진" value={data?.kpis?.avg_margin ?? '—'} unit="%" />
        <Metric label="영업 중 계정" value={active.length} unit={`/ ${channels.flatMap((c) => c.accounts).length}`} hint="나머지는 준비·신청·미연동" />
        <Metric label="등록 슬롯" value={fmt(freeSlots)} hint="한도가 정해진 계정 합" />
      </MetricStrip>

      <Card title="채널 × 계정" sub="칸을 누르면 그 컨텍스트로 이동합니다" padded={false}>
        <ChannelMatrix channels={channels} ordersByAccount={ordersByAccount} />
      </Card>
    </div>
  );
}

/** 4행(채널) × 2열(계정). 비어 있는 칸도 지우지 않는다 — 없다는 사실이 정보다. */
function ChannelMatrix({ channels, ordersByAccount }) {
  const navigate = useNavigate();
  const { withScope } = useChannel();

  if (!channels.length) {
    return <EmptyState title="채널 정보를 불러오지 못했습니다" description="/api/pa/accounts 응답을 확인하세요." />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-[640px] w-full text-[12.5px]">
        <thead>
          <tr className="bg-sunken text-2xs uppercase tracking-[0.06em] text-ink-400">
            <th className="border-b border-line px-3 py-2 text-left font-semibold">채널</th>
            <th className="border-b border-line px-3 py-2 text-center font-semibold">카리스 글로벌 (신)</th>
            <th className="border-b border-line px-3 py-2 text-center font-semibold">카리스G (구)</th>
          </tr>
        </thead>
        <tbody>
          {channels.map((c) => {
            const blocked = !c.usable;
            const byKey = (k) => c.accounts.find((a) => a.account_key === k);
            return (
              <tr key={c.channel} className={blocked ? 'opacity-60' : undefined}>
                <td className="border-b border-r border-line px-3 py-2.5 align-top">
                  <div className="flex items-center gap-2 text-[13px] font-semibold text-ink-900">
                    <ChannelMark channel={c.channel} mark={c.mark} muted={blocked} />
                    {c.label}
                  </div>
                  <div className="mt-1 pl-6 text-[11px] text-ink-400">
                    {c.accounts.map((a) => a.market_label).filter((v, i, arr) => arr.indexOf(v) === i).join(' · ')}
                  </div>
                </td>
                {['new', 'old'].map((key) => {
                  const a = byKey(key);
                  return (
                    <td key={key} className="border-b border-r border-line px-3 py-2.5 align-top last:border-r-0">
                      {!a ? (
                        <div className="text-[12px] text-ink-400">—</div>
                      ) : a.usable ? (
                        <button
                          type="button"
                          onClick={() => navigate(withScope('/', { channel: c.channel, account: key }))}
                          className="w-full text-left"
                        >
                          <div className="flex flex-wrap items-baseline gap-3">
                            <span className="flex items-baseline gap-1">
                              <span className="text-2xs text-ink-400">주문</span>
                              <span className="font-mono text-[14px] font-semibold tabular-nums text-ink-900">
                                {fmt(ordersByAccount[`${a.platform}:${key}`] || 0)}
                              </span>
                            </span>
                            {a.limit_products ? (
                              <span className="flex items-baseline gap-1">
                                <span className="text-2xs text-ink-400">한도</span>
                                <span className="font-mono text-[14px] font-semibold tabular-nums text-ink-900">
                                  {fmt(a.limit_products)}
                                </span>
                              </span>
                            ) : null}
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                            <StatusTag status={a.status} />
                            {a.store_name && <span className="text-2xs text-ink-400">{a.store_name}</span>}
                          </div>
                        </button>
                      ) : (
                        <div>
                          <div className="flex items-center gap-2 py-1 text-[12px] text-ink-400">
                            <span className="h-px flex-1 border-t border-dashed border-line-strong" />
                            {a.status === 'unknown' ? '공개 API 없음' : '계정 미연결'}
                            <span className="h-px flex-1 border-t border-dashed border-line-strong" />
                          </div>
                          <div className="mt-1 flex justify-center">
                            <StatusTag status={a.status} />
                          </div>
                        </div>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────
   채널 — 선택된 채널·계정
   ───────────────────────────────────────────────────────── */
function ChannelDashboard() {
  const navigate = useNavigate();
  const { channel, account, channelMeta, accountMeta, withScope } = useChannel();
  const { data } = useQuery({
    // ★queryKey 에 스코프를 넣어야 탭을 바꿀 때 다시 받아온다.
    queryKey: ['pa', 'dashboard', channel, account],
    queryFn: () => pa.dashboard({ channel, account }),
  });

  const [period, setPeriod] = React.useState('today');
  const ordersByAccount = data?.kpis?.orders_by_account || {};
  const myOrders = accountMeta ? (ordersByAccount[`${accountMeta.platform}:${account}`] || 0) : 0;

  const todos = data?.todos || {};
  const items = [
    todos.cancel_in_progress && {
      id: 'cancel', severity: 'crit', label: '취소 요청 진행 중',
      desc: '셀러센터 응대 필요', count: todos.cancel_in_progress, action: '주문',
      onClick: () => navigate(withScope('/orders')),
    },
    todos.amazon_purchase_no_id && {
      id: 'nopo', severity: 'crit', label: '발주번호 누락',
      desc: '아마존 구매 단계인데 주문번호가 비어 있음',
      count: todos.amazon_purchase_no_id, action: '주문',
      onClick: () => navigate(withScope('/orders')),
    },
    todos.upload_pending && {
      id: 'upload', severity: 'warn', label: '업로드 대기',
      count: todos.upload_pending, action: '등록 상품',
      onClick: () => navigate(withScope('/channel-products')),
    },
  ].filter(Boolean);

  return (
    <div className="grid gap-3">
      <PageHead
        title="대시보드"
        badge={
          <span className="inline-flex items-center gap-1.5">
            <ChannelMark channel={channel} mark={channelMeta.mark} />
            {channelMeta.label} · {accountMeta?.label || ''}
          </span>
        }
        sub={[accountMeta?.store_name, accountMeta?.vendor_id].filter(Boolean).join(' · ')}
        right={
          <>
            <Segmented
              value={period}
              onChange={setPeriod}
              options={[{ value: 'today', label: '오늘' }, { value: '7d', label: '7일' }, { value: '30d', label: '30일' }]}
            />
            <PrimaryAction status={accountMeta?.status} onClick={() => navigate(withScope('/channel-products'))} />
          </>
        }
      />

      {accountMeta && <AccountStateNote account={accountMeta} />}

      <TriageQueue items={items} total={items.reduce((s, i) => s + (Number(i.count) || 0), 0)} />

      <MetricStrip cols={5}>
        <Metric label="누적 주문" value={fmt(myOrders)} hint={accountMeta?.label} />
        <Metric
          label="판매중 상품"
          value={fmt(data?.kpis?.active_products)}
          hint={
            data?.kpis?.active_unknown
              ? `채널 확인 기준 · 미대조 ${fmt(data.kpis.active_unknown)}건`
              : '채널 확인 기준'
          }
        />
        <Metric label="평균 마진" value={data?.kpis?.avg_margin ?? '—'} unit="%" />
        <Metric
          label="등록 한도"
          value={accountMeta?.limit_products ? fmt(accountMeta.limit_products) : '없음'}
          hint={accountMeta?.limit_daily ? `일 ${fmt(accountMeta.limit_daily)}` : ''}
        />
        <Metric
          label="수수료"
          value={accountMeta?.fee_rate != null ? (accountMeta.fee_rate * 100).toFixed(2) : '—'}
          unit={accountMeta?.fee_rate != null ? '%' : ''}
        />
      </MetricStrip>

      <Card
        title="이 계정에서 할 수 있는 일"
        sub="계정 상태에 따라 주 행동이 달라집니다"
      >
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => navigate(withScope('/channel-products'))}>등록 상품 보기</Button>
          <Button onClick={() => navigate(withScope('/orders'))}>주문</Button>
          <Button onClick={() => navigate(withScope('/settlement'))}>정산</Button>
        </div>
      </Card>
    </div>
  );
}

/**
 * 주 버튼은 seller_accounts.status 가 결정한다.
 * 화면이 아니라 데이터가 상태의 단일 출처다.
 */
function PrimaryAction({ status, onClick }) {
  const label = {
    active: '등록 요청',
    ready: '재등록 시작',
    reducing: '정리 대상 보기',
    pending: '연동 확인',
    wiped: '재등록 시작',
  }[status] || '등록 상품';
  return <Button variant="primary" onClick={onClick}>{label}</Button>;
}

/** 계정 상태를 문맥으로 알린다 — 기능을 막지 않고, 지금 의미 있는 행동을 안내한다. */
function AccountStateNote({ account }) {
  const note = {
    ready: '연동은 끝났고 리스팅이 0입니다. 재등록 후보를 골라 시작하세요.',
    reducing: '등록 한도를 넘겼습니다. 신규 등록 전에 저마진 상품부터 정리해야 합니다.',
    pending: '판매 자격 신청 중입니다. 승인 전에는 등록이 막힙니다.',
    wiped: '상품이 전량 삭제된 상태입니다.',
  }[account.status];
  if (!note) return null;

  const overLimit = account.limit_products && account.status === 'reducing';
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border-l-2 border-signal-warn bg-surface px-3 py-2 text-[12.5px] text-ink-600">
      <StatusBadge variant={account.status === 'reducing' ? 'warn' : 'info'}>{account.status}</StatusBadge>
      <span>{note}</span>
      {overLimit && <span className="font-mono text-2xs text-ink-400">한도 {fmt(account.limit_products)}</span>}
      {account.note && <span className="text-ink-400">— {account.note}</span>}
    </div>
  );
}

/* ── 공통 조각 ─────────────────────────────────────────── */
function PageHead({ title, badge, sub, right }) {
  return (
    <header className="flex flex-wrap items-end gap-3">
      <div>
        <h1 className="flex items-center gap-2 text-[18px] font-semibold tracking-tight text-ink-900">
          {title}
          {badge && (
            <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-[11px] font-semibold text-accent">
              {badge}
            </span>
          )}
        </h1>
        {sub && <p className="mt-0.5 text-[12.5px] text-ink-400">{sub}</p>}
      </div>
      {right && <div className="ml-auto flex flex-wrap items-center gap-2">{right}</div>}
    </header>
  );
}

const STATUS_TONE = {
  active: 'ok', ready: 'info', reducing: 'warn',
  pending: 'mute', wiped: 'mute', unknown: 'mute',
};

function StatusTag({ status }) {
  if (!status) return null;
  return <StatusBadge variant={STATUS_TONE[status] || 'mute'}>{status}</StatusBadge>;
}

function fmt(v) {
  if (v == null) return '—';
  return Number(v).toLocaleString();
}
