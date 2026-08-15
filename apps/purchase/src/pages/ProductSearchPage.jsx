import React, { useState, useMemo, useRef, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card, StatusBadge } from '@charisg/ui';

// 채널코드 → 뱃지. gen_product_index.py 의 CODE 와 순서가 같아야 한다.
//   0=쿠팡 신  1=쿠팡 구  2=스토어 구(카리스G)  3=스토어 신(카리스 글로벌)  4=기타
// 스토어가 코드 하나였을 때는 구/신 계정이 한 뱃지로 뭉쳐 보였다(2026-08-10 분리).
const BADGES = [
  { label: '쿠팡 신계정', variant: 'ok' },
  { label: '쿠팡 구계정', variant: 'neutral' },
  { label: '스토어 카리스G', variant: 'neutral' },
  { label: '스토어 카리스 글로벌', variant: 'info' },
  { label: '기타', variant: 'warn' },
];
const CAP = 200; // 렌더 상한 (검색은 전체 대상)

function useProductIndex() {
  return useQuery({
    queryKey: ['pa', 'product-index'],
    queryFn: async () => {
      const res = await fetch(`${import.meta.env.BASE_URL}product-index.json`, { cache: 'no-cache' });
      if (!res.ok) throw new Error('상품 인덱스를 불러오지 못했습니다 (' + res.status + ')');
      return res.json();
    },
    staleTime: Infinity,
    retry: false,
  });
}

function Highlight({ text, q }) {
  if (!q) return text;
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return text;
  return (
    <>
      {text.slice(0, i)}
      <mark className="rounded-sm bg-accent/10 px-0.5 text-accent">{text.slice(i, i + q.length)}</mark>
      {text.slice(i + q.length)}
    </>
  );
}

function ResultRow({ item, q, copied, onCopy }) {
  const [name, asin, codes] = item;
  const url = 'https://www.amazon.com/dp/' + asin;
  return (
    <div className="flex flex-col gap-2 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="min-w-0 flex-1">
        <p className="break-words text-sm leading-snug text-ink-900">
          <Highlight text={name} q={q} />
        </p>
        <div className="mt-1.5 flex flex-wrap gap-1">
          {codes.map((c) => {
            const b = BADGES[c] || BADGES[BADGES.length - 1];
            return <StatusBadge key={c} variant={b.variant}>{b.label}</StatusBadge>;
          })}
        </div>
      </div>
      <div className="flex flex-shrink-0 items-center gap-2 sm:flex-col sm:items-end">
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-soft-warn px-3 text-xs font-semibold text-signal-warn ring-1 ring-inset ring-signal-warn/30 transition-colors hover:bg-soft-warn"
        >
          Amazon
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M7 17 17 7" /><path d="M9 7h8v8" /></svg>
        </a>
        <button
          type="button"
          onClick={() => onCopy(asin)}
          title="ASIN 복사"
          className={
            'inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 font-mono text-xs transition-colors ' +
            (copied === asin
              ? 'bg-soft-ok text-signal-ok ring-1 ring-inset ring-signal-ok/30'
              : 'bg-ink-50 text-ink-600 ring-1 ring-inset ring-ink-200 hover:ring-indigo-400')
          }
        >
          <span>{asin}</span>
          {copied === asin ? (
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
          ) : (
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
          )}
        </button>
      </div>
    </div>
  );
}

export default function ProductSearchPage() {
  const { data, isLoading, error } = useProductIndex();
  const [raw, setRaw] = useState('');
  const [q, setQ] = useState('');
  const [copied, setCopied] = useState(null);
  const debRef = useRef();

  const onChange = (e) => {
    const v = e.target.value;
    setRaw(v);
    clearTimeout(debRef.current);
    debRef.current = setTimeout(() => setQ(v.trim().toLowerCase().replace(/\s+/g, ' ')), 80);
  };

  const clear = () => {
    setRaw('');
    setQ('');
    clearTimeout(debRef.current);
  };

  const { results, total } = useMemo(() => {
    if (!data || !q) return { results: [], total: 0 };
    const out = [];
    let count = 0;
    for (let i = 0; i < data.length; i++) {
      const d = data[i];
      if (d[0].toLowerCase().indexOf(q) >= 0 || d[1].toLowerCase().indexOf(q) >= 0) {
        count++;
        if (out.length < CAP) out.push(d);
      }
    }
    return { results: out, total: count };
  }, [data, q]);

  const onCopy = useCallback((asin) => {
    navigator.clipboard.writeText(asin).then(() => {
      setCopied(asin);
      setTimeout(() => setCopied((c) => (c === asin ? null : c)), 1100);
    });
  }, []);

  const count = data ? data.length : 0;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-ink-900">상품 검색</h1>
        <p className="mt-0.5 text-sm text-ink-500">
          등록 상품명 또는 ASIN으로 검색하면 해당 상품의 ASIN과 Amazon 링크를 찾아줍니다.
        </p>
      </div>

      <Card padded={false}>
        <div className="p-4">
          <div className="relative">
            <svg className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-ink-400" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
            <input
              value={raw}
              onChange={onChange}
              type="search"
              autoComplete="off"
              spellCheck={false}
              autoFocus
              placeholder="상품명 또는 ASIN 입력 (예: 목베개, 루프랙, B0D115G688)"
              className="h-12 w-full rounded-lg border border-ink-200 bg-surface pl-11 pr-10 text-[15px] text-ink-900 placeholder:text-ink-400 focus:border-accent focus:outline-none focus:ring-2 focus:ring-brand-pa-100"
            />
            {raw && (
              <button
                type="button"
                onClick={clear}
                title="지우기"
                className="absolute right-3 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded-md bg-ink-100 text-ink-500 hover:bg-ink-200"
              >
                ✕
              </button>
            )}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 px-0.5 text-xs text-ink-500">
            {isLoading ? (
              <span>상품 인덱스 불러오는 중…</span>
            ) : error ? (
              <span className="text-signal-err">불러오기 실패 — 페이지를 새로고침 해주세요.</span>
            ) : q ? (
              <>
                <span>
                  <b className="font-semibold tabular-nums text-accent">{total.toLocaleString('ko')}</b>개 검색됨
                </span>
                {total > CAP && (
                  <>
                    <span className="text-ink-300">·</span>
                    <span>상위 {CAP}개 표시</span>
                  </>
                )}
              </>
            ) : (
              <>
                <span>
                  전체 <b className="font-semibold tabular-nums text-accent">{count.toLocaleString('ko')}</b>개 등록 상품
                </span>
                <span className="text-ink-300">·</span>
                <span>2026-07-24 기준 스냅샷</span>
              </>
            )}
          </div>
        </div>

        {q && !isLoading && !error && (
          <div className="border-t border-ink-100 px-4">
            {results.length === 0 ? (
              <div className="py-16 text-center">
                <p className="text-sm font-medium text-ink-600">일치하는 상품이 없습니다</p>
                <p className="mt-1 text-xs text-ink-400">등록 당시의 한글 상품명 기준입니다. 다른 키워드로 검색해 보세요.</p>
              </div>
            ) : (
              <>
                <div className="divide-y divide-ink-100">
                  {results.map((item) => (
                    <ResultRow key={item[1]} item={item} q={q} copied={copied} onCopy={onCopy} />
                  ))}
                </div>
                {total > CAP && (
                  <p className="py-3 text-center text-xs text-ink-400">
                    상위 {CAP}개만 표시 중 — 검색어를 더 구체적으로 입력하면 좁혀집니다.
                  </p>
                )}
              </>
            )}
          </div>
        )}

        {!q && !isLoading && !error && (
          <div className="border-t border-ink-100 py-16 text-center">
            <p className="text-sm font-medium text-ink-600">상품명 또는 ASIN으로 검색</p>
            <p className="mx-auto mt-1 max-w-md text-xs text-ink-400">
              쿠팡에 등록된 한글 상품명 일부(브랜드·키워드)나 Amazon ASIN을 입력하세요.
              같은 ASIN이 여러 채널에 등록됐으면 채널이 함께 표시됩니다.
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}
