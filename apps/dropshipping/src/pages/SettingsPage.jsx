import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, Button, StatusBadge } from '@charisg/ui';
import { useMarket } from '../App.jsx';

export default function SettingsPage() {
  const { market, ds } = useMarket();
  const qc = useQueryClient();
  const filters = useQuery({ queryKey: ['ds', 'filters', market], queryFn: ds.filters });
  const brands = useQuery({ queryKey: ['ds', 'brands', market], queryFn: ds.brands });
  const crawler = useQuery({ queryKey: ['ds', 'crawler'], queryFn: ds.crawlerStatus });

  const runCrawler = useMutation({
    mutationFn: (id) => ds.runCrawler({ crawler: id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ds', 'crawler'] }),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">설정</h1>
        <p className="mt-1 text-sm text-ink-500">Hard Filter, 차단 브랜드, 크롤러 관리.</p>
      </header>

      <Card title="Hard Filter 임계값">
        {filters.isLoading ? <div className="text-sm text-ink-400">로딩 중...</div> : (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {Object.entries(filters.data || {}).map(([k, v]) => (
              <div key={k} className="rounded-md border border-ink-200 bg-ink-50 px-3 py-2">
                <div className="text-xs text-ink-500">{k}</div>
                <div className="text-sm font-semibold text-ink-900">{String(v)}</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="차단 브랜드">
        {brands.isLoading ? <div className="text-sm text-ink-400">로딩 중...</div> : (
          <div className="flex flex-wrap gap-2">
            {(brands.data?.blocked_brands || []).map((b) => (
              <span key={b} className="rounded-md bg-ink-100 px-2 py-1 text-xs text-ink-700">{b}</span>
            ))}
          </div>
        )}
      </Card>

      <Card title="크롤러">
        {crawler.isLoading ? <div className="text-sm text-ink-400">로딩 중...</div> : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {(crawler.data?.crawlers || []).map((c) => (
              <div key={c.id} className="rounded-md border border-ink-200 bg-ink-50 p-4">
                <div className="text-sm font-semibold text-ink-900 mb-2">{c.label}</div>
                <div className="space-y-1 text-sm">
                  <div className="flex justify-between">
                    <span className="text-ink-500">수집 건수</span>
                    <span className="font-medium">{c.count?.toLocaleString() || '—'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-ink-500">상태</span>
                    <StatusBadge variant="info">{c.status}</StatusBadge>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-ink-500">마지막 실행</span>
                    <span>{c.last_run || '—'}</span>
                  </div>
                </div>
                <div className="mt-3">
                  <Button variant="ds" size="sm" onClick={() => runCrawler.mutate(c.id)} disabled={runCrawler.isPending}>
                    실행
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
