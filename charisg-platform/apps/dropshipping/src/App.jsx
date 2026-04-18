import React, { useEffect } from 'react';
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

  return (
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
        />
        <main className="flex-1 px-6 py-8">
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
  );
}
