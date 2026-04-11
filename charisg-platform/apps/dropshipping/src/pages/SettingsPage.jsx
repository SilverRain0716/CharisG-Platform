import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@charisg/ui';
import { ds } from '../api/ds.js';

export default function SettingsPage() {
  const filters = useQuery({ queryKey: ['ds', 'filters'], queryFn: ds.filters });
  const brands = useQuery({ queryKey: ['ds', 'brands'], queryFn: ds.brands });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">설정</h1>
        <p className="mt-1 text-sm text-ink-500">Hard Filter 8개 임계값, 차단 브랜드/카테고리, 크롤러 파라미터.</p>
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
    </div>
  );
}
