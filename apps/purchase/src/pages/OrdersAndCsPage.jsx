import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Card, LockChip } from '@charisg/ui';
import { pa } from '../api/pa.js';
import { useChannel } from '../channel.jsx';

// 주문 카드 채널 라벨 — 쿠팡은 구/신 계정 구분 표시
function ChannelTag({ item }) {
  if (item?.channel === 'coupang') {
    const isNew = item.coupang_account === 'new';
    return (
      <span className={isNew ? 'text-signal-info font-medium' : 'text-ink-500'}>
        {isNew ? '쿠팡·신(카리스)' : '쿠팡·구'}
      </span>
    );
  }
  return <span>{item?.channel}</span>;
}

// KanbanBoard 추상화 우회 — Tailwind class 만으로 모바일 세로 / 데스크탑 5컬럼.
// inline style, useEffect, matchMedia 모두 안 씀 → 첫 렌더부터 정확.
export default function OrdersAndCsPage() {
  const navigate = useNavigate();
  // 주문은 놓치면 안 되는 일이라 '전체' 탭에서는 합산을 그대로 보여준다.
  // 채널을 고른 상태에서만 그 계정으로 좁힌다. 통제점은 상단 계정 줄 하나뿐.
  const { channel, account, channelMeta, accountMeta } = useChannel();
  const scoped = channel !== 'all';
  const acctParam = scoped ? account : undefined;

  const kanban = useQuery({
    queryKey: ['pa', 'orders', 'kanban', channel, acctParam ?? 'all'],
    queryFn: () => pa.ordersKanban(acctParam),
  });

  // 배열이 아닌 응답(에러 봉투 등)이 한 번만 와도 아래 reduce 에서 화면 전체가 죽는다.
  const columns = Array.isArray(kanban.data) ? kanban.data : [];
  const total = columns.reduce((acc, c) => acc + (c.items?.length || 0), 0);

  return (
    <div className="space-y-4">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900">주문</h1>
          <p className="mt-1 text-sm text-ink-500">단계별 주문 진행 · 총 {total}건 (CS·반품은 별도 메뉴)</p>
        </div>
        {/* 범위는 상단에서 정해진다 — 여기서는 무엇으로 잠겼는지만 보여준다 */}
        <div className="flex items-center gap-2">
          {scoped ? (
            <LockChip title="상단 채널 탭·계정 줄에서 변경">
              {channelMeta.label}{accountMeta?.label ? ` · ${accountMeta.label}` : ''}
            </LockChip>
          ) : (
            <LockChip title="특정 계정만 보려면 상단에서 채널을 고르세요">전 채널·전 계정</LockChip>
          )}
        </div>
      </header>

      {kanban.isLoading && (
        <div className="rounded-xl bg-surface p-5 text-sm text-ink-500 ring-1 ring-ink-200">로딩 중…</div>
      )}
      {kanban.isError && (
        <div className="rounded-xl bg-soft-err p-5 text-sm text-signal-err ring-1 ring-signal-err/30">
          <div className="font-medium">주문 데이터를 불러오지 못했습니다.</div>
          <div className="mt-1 text-xs">
            상태: {kanban.error?.status || kanban.error?.message || 'unknown'}
            {kanban.error?.status === 401 && ' — 로그인이 만료됐습니다. 우측 상단에서 로그아웃 후 다시 로그인해주세요.'}
          </div>
        </div>
      )}
      {!kanban.isLoading && !kanban.isError && total === 0 && (
        <div className="rounded-xl bg-surface p-5 text-sm text-ink-500 ring-1 ring-ink-200">표시할 주문이 없습니다.</div>
      )}

      {!kanban.isLoading && total > 0 && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          {columns.map((col) => (
            <section key={col.id} className="rounded-lg bg-ink-50 p-3 ring-1 ring-ink-100">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-sm font-semibold text-ink-800">{col.label}</h3>
                <span className="rounded-full bg-surface px-2 py-0.5 text-xs font-medium text-ink-600 ring-1 ring-ink-200">
                  {col.items?.length || 0}
                </span>
              </div>
              {(col.items?.length || 0) === 0 ? (
                <div className="text-xs text-ink-400">—</div>
              ) : (
                <div className="space-y-2">
                  {col.items.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => navigate(`/orders/${item.id}`)}
                      className="block w-full rounded-md bg-surface p-3 text-left text-sm shadow-card ring-1 ring-ink-100 hover:shadow-card-hover"
                    >
                      <div className="font-medium text-ink-900 [word-break:keep-all]">
                        {item.customer_name || '(고객정보없음)'}
                      </div>
                      <div className="mt-0.5 text-xs text-ink-500">
                        <ChannelTag item={item} /> · ₩{item.sale_price_krw?.toLocaleString() || 0}
                      </div>
                      <div className="text-xs text-ink-400">{item.placed_at?.slice(0, 10)}</div>
                    </button>
                  ))}
                </div>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
