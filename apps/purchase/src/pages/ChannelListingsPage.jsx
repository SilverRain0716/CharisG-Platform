import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@charisg/auth';
import { Card, DataTable, StatusBadge, EmptyState } from '@charisg/ui';

import { useChannel } from '../channel.jsx';

/**
 * 채널 공용 리스팅 목록.
 *
 * 쿠팡·네이버 화면이 각자 진화해 서로 다른 코드가 됐다. 그런데 목록이 보여줘야 하는 것
 * (상품명·가격·상태·옵션)은 채널이 달라도 같다. 11번가·옥션 화면을 또 따로 만들면
 * 네 벌이 되고, 어긋남도 네 배가 된다. 그래서 하나로 둔다.
 *
 * ★반드시 두 상태를 나란히 보여준다 —
 *     우리 판정(status) 과 채널 원문(channel_status)
 *   2026-08-15 실측에서 네이버 18건이 우리 DB 로는 paused 인데 채널에선 전부 SALE 이었다.
 *   한 칸만 보여주면 이런 걸 영영 못 본다.
 */
const STATE_TABS = [
  { key: 'live', label: '살아있음' },
  { key: 'selling', label: '채널 판매중' },
  { key: 'all', label: '전체' },
];

export default function ChannelListingsPage() {
  const { channel, account, channelMeta } = useChannel();
  const [state, setState] = React.useState('live');
  const [q, setQ] = React.useState('');

  const params = new URLSearchParams({ state, limit: '200' });
  if (channel && channel !== 'all') params.set('channel', channel);
  if (account) params.set('account', account);
  if (q) params.set('q', q);

  const list = useQuery({
    queryKey: ['pa', 'listings', channel, account, state, q],
    queryFn: () => apiFetch(`/api/pa/listings?${params.toString()}`),
    keepPreviousData: true,
  });

  const rows = list.data?.rows || [];

  return (
    <div className="space-y-4">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">
            {channelMeta?.label || channel} 등록 상품
          </h1>
          <p className="mt-1 text-sm text-ink-500">
            채널이 진실이다 — 우리 판정과 채널 원문을 나란히 둔다.
          </p>
        </div>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="상품명 · ASIN · 상품번호"
          className="h-9 w-64 rounded-md border border-ink-200 px-3 text-sm"
        />
      </header>

      <div className="flex gap-2">
        {STATE_TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setState(t.key)}
            className={`rounded-md px-3 py-1.5 text-sm ${
              state === t.key ? 'bg-ink-900 text-white' : 'bg-ink-50 text-ink-600'
            }`}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto self-center text-sm text-ink-500">
          {list.data?.total ?? '—'}건
        </span>
      </div>

      <Card padded={false}>
        {!list.isLoading && rows.length === 0 ? (
          <EmptyState title="해당하는 리스팅이 없습니다" description="상태 탭이나 검색어를 바꿔 보세요." />
        ) : (
          <DataTable
            columns={[
              { key: 'channel_product_id', label: '상품번호', width: '130px',
                render: (v, r) => r.list_url
                  ? <a href={r.list_url} target="_blank" rel="noreferrer" className="text-brand-600 underline">{v}</a>
                  : v },
              { key: 'title_ko', label: '상품명' },
              { key: 'asin', label: 'ASIN', width: '110px' },
              { key: 'status', label: '우리 판정', width: '110px',
                render: (v) => <StatusBadge variant={v === 'listed' ? 'info' : 'mute'}>{v}</StatusBadge> },
              { key: 'channel_status', label: '채널 원문', width: '110px',
                // ★채널이 진실이다. 우리 판정과 다르면 눈에 띄게 한다.
                render: (v, r) => v == null
                  ? <span className="text-ink-400">미대조</span>
                  : <StatusBadge variant={r.selling ? 'success' : 'warn'}>{v}</StatusBadge> },
              { key: 'sale_krw', label: '판매가', width: '100px',
                render: (v) => v ? `${Number(v).toLocaleString()}원` : '—' },
              { key: 'option_total', label: '옵션', width: '90px',
                // 옵션ID 가 없으면 주문이 와도 어느 자식인지 모른다 → 오배송
                render: (v, r) => !v ? <span className="text-ink-400">단품</span>
                  : <span className={r.option_gap ? 'font-medium text-danger-600' : ''}>
                      {r.option_with_id}/{v}
                    </span> },
              { key: 'channel_checked_at', label: '대조', width: '150px',
                render: (v) => v ? v.replace('T', ' ').slice(0, 16) : <span className="text-ink-400">—</span> },
            ]}
            rows={rows}
            rowKey={(r) => r.id}
            pageSize={50}
          />
        )}
      </Card>
    </div>
  );
}
