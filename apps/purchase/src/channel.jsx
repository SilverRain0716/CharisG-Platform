import React, { createContext, useCallback, useContext, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@charisg/auth';

/**
 * 채널 × 계정 컨텍스트.
 *
 * 콘솔 전체가 "지금 어느 채널의 어느 계정을 보고 있는가" 하나에 매여 있다.
 * 목록·주문·정산이 각자 채널 필터를 들고 있으면 서로 어긋나므로, 그 값을
 * 여기 한 곳에서만 바꾼다(상단 채널 탭 + 계정 줄).
 *
 * ★위치는 URL 쿼리(?ch=&acct=)다. 경로 세그먼트(/coupang/new/orders)로 두면
 *   더 깔끔하지만 기존 17개 화면의 경로와 내부 navigate 를 전부 갈아야 해서,
 *   같은 목적(새로고침·북마크·링크 공유에 컨텍스트가 남는다)을 만족하는 쪽으로
 *   골랐다. 경로 승격은 화면 이관이 끝난 뒤에 해도 늦지 않다.
 *
 * 채널 목록은 하드코딩하지 않고 /api/pa/accounts(= seller_accounts)에서 받는다.
 * 11번가 두 번째 계정이 붙거나 ESM API 가 열리면 DB 한 줄로 화면이 따라온다.
 */

const ChannelContext = createContext(null);
const LAST_KEY = 'charisg_last_scope';   // { channel, accountByChannel: {ch: acct} }

const ALL = { channel: 'all', label: '전체', mark: 'ALL' };

function readLast() {
  try {
    return JSON.parse(localStorage.getItem(LAST_KEY) || '{}');
  } catch {
    return {};
  }
}

function writeLast(next) {
  try {
    localStorage.setItem(LAST_KEY, JSON.stringify(next));
  } catch {
    /* 저장 실패해도 URL 이 진실이라 동작에는 지장 없다 */
  }
}

export function ChannelProvider({ children }) {
  const [params, setParams] = useSearchParams();

  const { data, isLoading } = useQuery({
    queryKey: ['pa', 'accounts'],
    queryFn: () => apiFetch('/api/pa/accounts'),
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const channels = data?.channels || [];
  const last = readLast();

  // 채널 결정 — URL > 마지막으로 본 채널 > 쓸 수 있는 첫 채널 > 전체
  const urlChannel = params.get('ch');
  const known = (c) => c === 'all' || channels.some((x) => x.channel === c);
  const channel = known(urlChannel) ? urlChannel
    : known(last.channel) ? last.channel
      : (channels.find((c) => c.usable)?.channel || 'all');

  const channelMeta = channel === 'all'
    ? { ...ALL, accounts: [], usable: true }
    : channels.find((c) => c.channel === channel) || { ...ALL, accounts: [], usable: false };

  // 계정 결정 — URL > 그 채널에서 마지막으로 본 계정 > 쓸 수 있는 첫 계정
  const accounts = channelMeta.accounts || [];
  const urlAccount = params.get('acct');
  const hasAcct = (a) => accounts.some((x) => x.account_key === a);
  const account = channel === 'all'
    ? (urlAccount === 'new' || urlAccount === 'old' ? urlAccount : 'both')
    : hasAcct(urlAccount) ? urlAccount
      : hasAcct(last.accountByChannel?.[channel]) ? last.accountByChannel[channel]
        : (accounts.find((a) => a.usable)?.account_key || accounts[0]?.account_key || 'new');

  const accountMeta = accounts.find((a) => a.account_key === account) || null;

  const apply = useCallback((nextChannel, nextAccount) => {
    const p = new URLSearchParams(params);
    p.set('ch', nextChannel);
    if (nextAccount) p.set('acct', nextAccount);
    else p.delete('acct');
    setParams(p, { replace: false });

    const prev = readLast();
    writeLast({
      channel: nextChannel,
      accountByChannel: { ...(prev.accountByChannel || {}), ...(nextAccount ? { [nextChannel]: nextAccount } : {}) },
    });
  }, [params, setParams]);

  const setChannel = useCallback((next) => {
    // 채널을 바꿀 때 계정은 그 채널에서 마지막으로 보던 것으로 복원한다.
    // 채널마다 쓰는 계정이 달라서(쿠팡은 신, 11번가는 신뿐) 일괄로 끌고 다니면 헛다리를 짚는다.
    const prev = readLast();
    const remembered = prev.accountByChannel?.[next];
    const target = channels.find((c) => c.channel === next);
    const fallback = target?.accounts?.find((a) => a.usable)?.account_key
      || target?.accounts?.[0]?.account_key;
    apply(next, next === 'all' ? null : (remembered || fallback));
  }, [apply, channels]);

  const setAccount = useCallback((next) => apply(channel, next), [apply, channel]);

  /** 링크에 현재 컨텍스트를 실어 보낸다 — 페이지 간 이동에서 채널이 풀리지 않게 */
  const withScope = useCallback((path, override) => {
    const ch = override?.channel || channel;
    const acct = override?.account || (ch === channel ? account : undefined);
    const qs = new URLSearchParams();
    qs.set('ch', ch);
    if (acct && ch !== 'all') qs.set('acct', acct);
    return `${path}?${qs.toString()}`;
  }, [channel, account]);

  const value = useMemo(() => ({
    loading: isLoading,
    channels,
    channel,
    account,
    channelMeta,
    accountMeta,
    accounts,
    setChannel,
    setAccount,
    withScope,
    /** React Query 키 접두사. 계정만 다른 같은 화면이 서로의 캐시를 보여주는 사고를 막는다. */
    scope: [channel, account],
  }), [isLoading, channels, channel, account, channelMeta, accountMeta, accounts, setChannel, setAccount, withScope]);

  return <ChannelContext.Provider value={value}>{children}</ChannelContext.Provider>;
}

export function useChannel() {
  const ctx = useContext(ChannelContext);
  if (!ctx) throw new Error('useChannel 은 ChannelProvider 안에서만 쓸 수 있다');
  return ctx;
}

/** 채널 코드 → CSS 액센트 스코프. tokens.css 의 [data-channel] 규칙과 짝이다. */
export function channelAttr(channel) {
  return { 'data-channel': channel || 'all' };
}
