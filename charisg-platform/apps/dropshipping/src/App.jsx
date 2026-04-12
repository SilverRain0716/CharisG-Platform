import React, { useEffect } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { GlobalTopBar, Sidebar } from '@charisg/ui';
import { apiFetch, useAuth } from '@charisg/auth';

import PipelineOverview from './pages/PipelineOverview.jsx';
import ScoringDashboard from './pages/ScoringDashboard.jsx';
import ProductList from './pages/ProductList.jsx';
import PriceCompetitiveness from './pages/PriceCompetitiveness.jsx';
import ListingStatus from './pages/ListingStatus.jsx';
import AccountHealth from './pages/AccountHealth.jsx';
import CrawlerManagement from './pages/CrawlerManagement.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import Performance from './pages/Performance.jsx';

const NAV = [
  { id: 'overview',  href: '/',                   label: '대시보드' },
  { id: 'scoring',   href: '/scoring',            label: '스코어링' },
  { id: 'products',  href: '/products',           label: '상품 목록' },
  { id: 'price',     href: '/price',              label: '가격 경쟁력' },
  { id: 'kanban',    href: '/kanban',             label: '리스팅 칸반' },
  { id: 'health',    href: '/health',             label: '계정 건강도' },
  { id: 'crawler',   href: '/crawler',            label: '크롤러' },
  { id: 'settings',  href: '/settings',           label: '설정' },
  { id: 'performance', href: '/performance',      label: '매출·성과 (Phase 1+)' },
];

export default function App() {
  const { user, loading, logout } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  const { data: summary } = useQuery({
    queryKey: ['hub', 'summary'],
    queryFn: () => apiFetch('/api/hub/summary'),
    enabled: !!user,
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
            <Route path="/price" element={<PriceCompetitiveness />} />
            <Route path="/kanban" element={<ListingStatus />} />
            <Route path="/health" element={<AccountHealth />} />
            <Route path="/crawler" element={<CrawlerManagement />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/performance" element={<Performance />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
