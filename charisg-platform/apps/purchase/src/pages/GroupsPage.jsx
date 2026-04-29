import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Card, DataTable, StatusBadge, Button } from '@charisg/ui';
import { pa } from '../api/pa.js';

export default function GroupsPage({ showHeader = true } = {}) {
  const navigate = useNavigate();
  const [channel, setChannel] = useState('coupang');
  const [filter, setFilter] = useState('all'); // all|over|under

  const stats = useQuery({
    queryKey: ['pa', 'groups', 'stats', channel],
    queryFn: () => pa.groupsStats(channel),
  });

  const list = useQuery({
    queryKey: ['pa', 'groups', 'list', channel, filter],
    queryFn: () => pa.groupsList({
      channel,
      ...(filter === 'over' ? { over_limit: true } : {}),
      ...(filter === 'under' ? { over_limit: false } : {}),
      limit: 100,
    }),
  });

  const items = list.data?.items || [];

  return (
    <div className="space-y-6">
      {showHeader && (
        <header>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">옵션 그룹</h1>
          <p className="mt-1 text-sm text-ink-500">
            parent ASIN 단위로 묶인 SKU 다발. 30개 초과는 자동 분리 등록 후보.
          </p>
        </header>
      )}

      {/* 통계 카드 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="전체 그룹" value={stats.data?.total_groups || 0} />
        <StatCard label={`한도 초과 (${channel})`} value={stats.data?.over_limit || 0} accent />
        <StatCard label="한도 이하 (단일)" value={stats.data?.under_limit || 0} />
        <StatCard label="평균 옵션 수" value={stats.data?.avg_child_count || 0} />
        <StatCard label="최대 옵션 수" value={stats.data?.max_child_count || 0} />
      </div>

      {/* 컨트롤 바 */}
      <Card padded>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-2">
            <span className="text-xs text-ink-500 self-center mr-2">채널</span>
            {['coupang', 'smartstore'].map((c) => (
              <button
                key={c}
                onClick={() => setChannel(c)}
                className={`px-3 py-1.5 rounded-full text-xs border transition ${
                  channel === c
                    ? 'bg-pa-500 text-white border-pa-500'
                    : 'bg-white text-ink-600 border-ink-200 hover:border-pa-300'
                }`}
              >
                {c} (한도 {c === 'coupang' ? 30 : 100})
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <span className="text-xs text-ink-500 self-center mr-2">필터</span>
            {[
              { k: 'all', label: '전체' },
              { k: 'over', label: '한도 초과 (분리 필요)' },
              { k: 'under', label: '한도 이하' },
            ].map((f) => (
              <button
                key={f.k}
                onClick={() => setFilter(f.k)}
                className={`px-3 py-1.5 rounded-full text-xs border transition ${
                  filter === f.k
                    ? 'bg-ink-700 text-white border-ink-700'
                    : 'bg-white text-ink-600 border-ink-200'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
      </Card>

      {/* 그룹 목록 */}
      <Card title={`${list.data?.total || 0}개 그룹 (${items.length} 표시)`} padded={false}>
        {list.isLoading ? (
          <div className="p-8 text-center text-sm text-ink-400">로딩 중...</div>
        ) : (
          <DataTable
            columns={[
              { key: 'parent_asin', label: 'Parent ASIN', width: '130px',
                render: (v) => <span className="font-mono text-xs">{v}</span> },
              { key: 'brand', label: '브랜드', width: '140px' },
              { key: 'base_name_en', label: '상품명', wrap: true, maxWidth: '380px',
                render: (v) => <span className="text-xs">{v || '—'}</span> },
              { key: 'variation_theme', label: '차원', width: '170px',
                render: (v) => <span className="text-[11px] font-mono text-ink-500">{v || '—'}</span> },
              { key: 'child_count', label: '옵션 수', width: '90px',
                render: (v) => {
                  const limit = stats.data?.channel_limit || 30;
                  const over = v > limit;
                  return (
                    <StatusBadge variant={over ? 'warn' : 'ok'}>
                      {v} {over ? '⚠' : ''}
                    </StatusBadge>
                  );
                } },
              { key: 'ingestion_status', label: '상태', width: '110px' },
              { key: '_action', label: '', width: '90px',
                render: (_, row) => (
                  <Button size="sm" variant="ghost"
                    onClick={() => navigate(`/groups/${row.parent_asin}?channel=${channel}`)}>
                    상세 →
                  </Button>
                ) },
            ]}
            rows={items}
            rowKey={(r) => r.parent_asin}
            pageSize={50}
          />
        )}
      </Card>
    </div>
  );
}

function StatCard({ label, value, accent = false }) {
  return (
    <div className={`rounded-lg border p-3 ${accent ? 'bg-amber-50 border-amber-200' : 'bg-white border-ink-200'}`}>
      <div className="text-[11px] text-ink-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${accent ? 'text-amber-700' : 'text-ink-900'}`}>
        {Number(value).toLocaleString()}
      </div>
    </div>
  );
}
