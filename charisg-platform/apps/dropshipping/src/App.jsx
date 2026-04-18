import React, { useEffect, useState, useMemo, createContext, useContext } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { GlobalTopBar, Sidebar } from '@charisg/ui';
import { apiFetch, useAuth } from '@charisg/auth';

import PipelineOverview from './pages/PipelineOverview.jsx';
import ScoringDashboard from './pages/ScoringDashboard.jsx';
import ProductList from './pages/ProductList.jsx';
import MyListings from './pages/MyListings.jsx';
import AccountHealth from './pages/AccountHealth.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import { createDsApi } from './api/ds.js';

const MARKETS = [
  { id: 'US', label: 'US', flag: '🇺🇸' },
  { id: 'CA', label: 'Canada', flag: '🇨🇦' },
  { id: 'MX', label: 'Mexico', flag: '🇲🇽' },
];

// 마켓 컨텍스트 — 하위 페이지에서 useMarket()으로 접근
const MarketContext = createContext({ market: 'US', ds: null });
export const useMarket = () => useContext(MarketContext);

const NAV = [
  { id: 'overview',  href: '/',          label: '대시보드' },
  { id: 'scoring',   href: '/scoring',   label: '스코어링' },
  { id: 'products',  href: '/products',  label: '상품 후보' },
  { id: 'listings',  href: '/listings',  label: '내 리스팅' },
  { id: 'health',    href: '/health',    label: '계정 건강도' },
  { id: 'settings',  href: '/settings',  label: '설정' },
];

export default function App() {
  const { user, loading, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [market, setMarket] = useState(() => localStorage.getItem('ds_market') || 'US');

  // market 변경 시 localStorage 저장
  useEffect(() => { localStorage.setItem('ds_market', market); }, [market]);

  // 마켓별 API 인스턴스
  const dsApi = useMemo(() => createDsApi(market), [market]);

  const { data: summary } = useQuery({
    queryKey: ['hub', 'summary'],
    queryFn: () => apiFetch('/api/hub/summary'),
    enabled: !!user,
    retry: false,
  });

  useEffect(() => {
    if (!loading && !user) {
      window.location.replace('/login?next=' + encodeURIComponent('/dropshipping/'));
    }
  }, [loading, user]);

  if (loading || !user) {
    return <div className="flex h-screen items-center justify-center text-ink-500">로딩 중...</div>;
  }

  const items = NAV.map((n) => ({
    ...n,
    active: n.href === '/' ? location.pathname === '/' : location.pathname.startsWith(n.href),
  }));

  const marketInfo = MARKETS.find((m) => m.id === market) || MARKETS[0];

  return (
    <MarketContext.Provider value={{ market, ds: dsApi }}>
      <div className="min-h-screen bg-ink-50">
        <GlobalTopBar
          activeApp="dropshipping"
          summary={summary}
          user={user}
          onLogout={logout}
          onLogoClick={() => (window.location.href = '/')}
        />
        <div className="mx-auto flex max-w-[1600px]">
          <Sidebar
            theme="ds"
            items={items}
            onSelect={(id) => {
              const item = items.find((i) => i.id === id);
              if (item) navigate(item.href);
            }}
            header={
              <div>
                <label className="block text-xs font-medium text-ink-500 mb-1">마켓플레이스</label>
                <select
                  value={market}
                  onChange={(e) => setMarket(e.target.value)}
                  className="w-full rounded-md border border-ink-300 bg-white px-2 py-1.5 text-sm font-semibold text-ink-900 focus:border-ds-500 focus:ring-1 focus:ring-ds-500"
                >
                  {MARKETS.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.flag} {m.label} ({m.id})
                    </option>
                  ))}
                </select>
              </div>
            }
          />
          <main className="flex-1 px-6 py-8">
            <div className="mb-4 flex items-center gap-2">
              <span className="text-lg">{marketInfo.flag}</span>
              <span className="text-sm font-medium text-ink-500">
                Amazon {marketInfo.label}
              </span>
            </div>
            <Routes>
              <Route path="/" element={<PipelineOverview />} />
              <Route path="/scoring" element={<ScoringDashboard />} />
              <Route path="/products" element={<ProductList />} />
              <Route path="/listings" element={<MyListings />} />
              <Route path="/health" element={<AccountHealth />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </main>
        </div>
      </div>
    </MarketContext.Provider>
  );
}
