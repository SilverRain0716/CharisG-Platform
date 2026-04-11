import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { GlobalTopBar, KPICard, Card, Button, AlertFeed } from '@charisg/ui';
import { apiFetch, useAuth } from '@charisg/auth';

export default function HubDashboard() {
  const { user, logout } = useAuth();

  const { data: summary, isLoading } = useQuery({
    queryKey: ['hub', 'summary'],
    queryFn: () => apiFetch('/api/hub/summary'),
  });

  const ds = summary?.ds || {};
  const pa = summary?.pa || {};

  return (
    <div className="min-h-screen bg-ink-50">
      <GlobalTopBar
        activeApp="hub"
        summary={summary}
        user={user}
        onLogout={logout}
        onLogoClick={() => (window.location.href = '/')}
      />

      <main className="mx-auto max-w-[1600px] px-6 py-8">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">Hub</h1>
          <p className="mt-1 text-sm text-ink-500">
            드랍쉬핑과 구매대행 양쪽 비즈니스를 한눈에.
          </p>
        </div>

        {isLoading && (
          <div className="rounded-lg bg-white p-10 text-center text-sm text-ink-400 ring-1 ring-ink-100">
            요약 로딩 중...
          </div>
        )}

        {!isLoading && (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <SummaryCard
              title="Dropshipping"
              accent="ds"
              hrefLabel="Open Dropshipping →"
              href="/dropshipping/"
              kpis={[
                { label: '활성 상품',  value: fmt(ds.active_products), accent: 'ds' },
                { label: '총 매출',    value: fmtUsd(ds.total_revenue), accent: 'ds' },
                { label: '평균 마진',  value: fmtPct(ds.avg_margin),   accent: 'ds' },
                { label: 'GO 건수',    value: fmt(ds.go_count),         accent: 'ds' },
                { label: '미처리',     value: fmt(ds.pendingCount),     accent: 'ds' },
                { label: '계정건강도', value: ds.account_health || '—', accent: 'ds' },
              ]}
            />
            <SummaryCard
              title="Purchase Agent"
              accent="pa"
              hrefLabel="Open Purchase Agent →"
              href="/purchase/"
              kpis={[
                { label: '활성 상품',  value: fmt(pa.active_products),   accent: 'pa' },
                { label: '월 매출',    value: fmtKrw(pa.monthly_revenue), accent: 'pa' },
                { label: '평균 마진',  value: fmtPct(pa.avg_margin),     accent: 'pa' },
                { label: '대기 주문',  value: fmt(pa.pending_orders),     accent: 'pa' },
                { label: '미처리 CS',  value: fmt(pa.pending_cs),         accent: 'pa' },
                { label: '미처리',     value: fmt(pa.pendingCount),       accent: 'pa' },
              ]}
            />
          </div>
        )}
      </main>
    </div>
  );
}

function SummaryCard({ title, accent, kpis, href, hrefLabel }) {
  return (
    <Card
      title={title}
      action={
        <Button
          variant={accent === 'ds' ? 'ds' : 'pa'}
          size="sm"
          onClick={() => (window.location.href = href)}
        >
          {hrefLabel}
        </Button>
      }
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {kpis.map((k, i) => (
          <KPICard key={i} {...k} />
        ))}
      </div>
    </Card>
  );
}

function fmt(v) {
  if (v == null) return '—';
  return Number(v).toLocaleString();
}
function fmtUsd(v) {
  if (v == null) return '—';
  return '$' + Number(v).toLocaleString();
}
function fmtKrw(v) {
  if (v == null) return '—';
  return '₩' + Number(v).toLocaleString();
}
function fmtPct(v) {
  if (v == null) return '—';
  return Number(v).toFixed(1) + '%';
}
