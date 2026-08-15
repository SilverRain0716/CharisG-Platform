import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, KPICard, StatusBadge } from '@charisg/ui';
import { pa } from '../api/pa.js';

function GateChip({ check }) {
  if (!check) return <span className="text-ink-300">—</span>;
  const st = check.status;
  if (st === '통과') return <StatusBadge variant="ok">통과</StatusBadge>;
  if (st === '대기') return <StatusBadge variant="neutral">대기</StatusBadge>;
  if (st === '수동검토') return <StatusBadge variant="warn">{`수동검토 · ${check.hits || 0}건`}</StatusBadge>;
  return <StatusBadge variant="err">{check.matched ? `차단: ${check.matched}` : '차단'}</StatusBadge>;
}

const FILTERS = [
  { id: 'all', label: '전체' },
  { id: 'blocked', label: '차단·검수기록' },
  { id: 'pass', label: '통과' },
];

export default function ScreeningPage() {
  const overview = useQuery({ queryKey: ['pa', 'ip-screening'], queryFn: pa.ipScreening, retry: false });

  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const [qInput, setQInput] = useState('');

  const list = useQuery({
    queryKey: ['pa', 'ip-screening-list', page, filter, q],
    queryFn: () => pa.ipScreeningList({ page, page_size: 50, filter, q }),
    placeholderData: (prev) => prev,
    retry: false,
  });

  const s = overview.data?.summary;
  const demo = overview.data?.kipris_demo;
  const ld = list.data;

  const setF = (f) => { setFilter(f); setPage(1); };
  const doSearch = () => { setQ(qInput.trim()); setPage(1); };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-900">
          지식재산권(IP) 사전검수
          <StatusBadge variant="ok" className="ml-2 align-middle">● LIVE</StatusBadge>
        </h1>
        <p className="mt-1 text-sm text-ink-500">
          상품 등록 전 4중 IP 게이트 자동 검수 — 브랜드 게이트 · 정책/IP 키워드 · 한국 제조사 · KIPRIS 국내 등록 IP 라이브 대조
        </p>
      </header>

      {s && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
          <KPICard accent="pa" label="검수 대상 상품" value={s.catalog_total.toLocaleString()} hint="전체 카탈로그" />
          <KPICard accent="pa" label="IP·정책 차단 누적" value={s.flagged_total.toLocaleString()} hint="사전 차단·수동검토" />
          <KPICard accent="pa" label="브랜드 게이트" value={s.blocklist_keywords} hint="정품 게이팅 키워드" />
          <KPICard accent="pa" label="정책·IP 키워드" value={s.ip_keywords} hint="저작권/상표/캐릭터" />
          <KPICard accent="pa" label="KIPRIS 연동" value={s.kipris_enabled ? '라이브' : '대기'} hint="한국특허정보원 API" />
        </div>
      )}

      {demo && (
        <Card title="KIPRIS 라이브 조회 — 한국특허정보원 권리자 대조">
          <div className="flex flex-wrap items-center gap-3">
            <div className="text-sm text-ink-600">질의 브랜드 <b className="text-accent">"{demo.query}"</b></div>
            <StatusBadge variant="warn">{`⚠ ${demo.status} — 국내 등록 권리자 ${(demo.matches || []).length}건`}</StatusBadge>
          </div>
          <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
            {(demo.matches || []).slice(0, 6).map((m, i) => (
              <div key={i} className="border-t border-ink-50 py-1.5">
                <div className="text-sm font-semibold text-ink-800">{m.name}</div>
                <div className="text-xs text-ink-400">권리자번호 {m.person_number}</div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card
        title="전체 상품 IP 검수 목록"
        padded={false}
        action={
          <div className="flex items-center gap-2">
            <input
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && doSearch()}
              placeholder="브랜드 또는 ASIN 검색"
              className="w-48 rounded-md border border-ink-200 px-2.5 py-1 text-sm outline-none focus:border-brand-pa-400"
            />
            <button onClick={doSearch} className="rounded-md bg-accent px-3 py-1 text-sm font-medium text-white">검색</button>
          </div>
        }
      >
        <div className="flex items-center justify-between border-b border-ink-100 px-4 py-2">
          <div className="flex gap-1">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                onClick={() => setF(f.id)}
                className={`rounded-md px-3 py-1 text-sm font-medium ${filter === f.id ? 'bg-accent text-white' : 'text-ink-500 hover:bg-ink-50'}`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="text-sm text-ink-500">
            {ld ? `총 ${ld.total.toLocaleString()}건 · ${ld.page.toLocaleString()} / ${ld.total_pages.toLocaleString()} 페이지` : '…'}
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-ink-100 bg-ink-50 text-left text-xs font-semibold text-ink-500">
                <th className="px-3 py-2">ASIN</th>
                <th className="px-3 py-2">브랜드</th>
                <th className="px-3 py-2">상품명</th>
                <th className="px-3 py-2">브랜드 게이트</th>
                <th className="px-3 py-2">정책·IP 키워드</th>
                <th className="px-3 py-2">한국 제조사</th>
                <th className="px-3 py-2">KIPRIS</th>
                <th className="px-3 py-2">판정</th>
                <th className="px-3 py-2">검수 기록</th>
              </tr>
            </thead>
            <tbody>
              {list.isError && (
                <tr><td colSpan={9} className="px-3 py-10 text-center text-signal-err">목록을 불러오지 못했습니다.</td></tr>
              )}
              {!list.isError && (ld?.rows || []).map((r) => (
                <tr key={r.asin} className={`border-b border-ink-50 ${r.blocked ? 'bg-soft-err/40' : ''}`}>
                  <td className="px-3 py-2 font-mono text-xs text-ink-500">{r.asin}</td>
                  <td className="px-3 py-2 font-semibold text-ink-800">{r.brand}</td>
                  <td className="max-w-[260px] truncate px-3 py-2 text-ink-600">{r.title_ko}</td>
                  <td className="px-3 py-2"><GateChip check={r.checks.brand_gate} /></td>
                  <td className="px-3 py-2"><GateChip check={r.checks.ip_keyword} /></td>
                  <td className="px-3 py-2"><GateChip check={r.checks.korean_mfr} /></td>
                  <td className="px-3 py-2"><GateChip check={r.checks.kipris} /></td>
                  <td className="px-3 py-2">
                    <StatusBadge variant={r.blocked ? 'err' : 'ok'}>{r.blocked ? '차단·수동검토' : '통과'}</StatusBadge>
                  </td>
                  <td className="px-3 py-2 text-xs text-ink-400">
                    {r.violation_keyword ? `${r.violation_flags || '위반'} / ${r.violation_keyword}` : '—'}
                  </td>
                </tr>
              ))}
              {!list.isError && ld && ld.rows.length === 0 && (
                <tr><td colSpan={9} className="px-3 py-10 text-center text-ink-400">결과 없음</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center justify-between border-t border-ink-100 px-4 py-3">
          <div className="text-xs text-ink-400">
            {list.isFetching ? '불러오는 중…' : `브랜드 게이트·정책/IP 키워드·한국 제조사 게이트는 실시간 검수. KIPRIS는 브랜드별 국내 등록 IP를 사전 조회·캐시하여 표시(미조회 브랜드는 '대기', 쿼터 보호).`}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="rounded-md border border-ink-200 px-3 py-1 text-sm disabled:opacity-40"
            >이전</button>
            <span className="text-sm text-ink-600">{ld ? `${ld.page} / ${ld.total_pages}` : '—'}</span>
            <button
              onClick={() => setPage((p) => (ld && p < ld.total_pages ? p + 1 : p))}
              disabled={!ld || page >= ld.total_pages}
              className="rounded-md border border-ink-200 px-3 py-1 text-sm disabled:opacity-40"
            >다음</button>
          </div>
        </div>
      </Card>
    </div>
  );
}
