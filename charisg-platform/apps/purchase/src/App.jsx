import React from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { GlobalTopBar, Sidebar } from '@charisg/ui';
import { apiFetch, useAuth } from '@charisg/auth';

import DashboardPage from './pages/DashboardPage.jsx';
import DiscoveryPage from './pages/DiscoveryPage.jsx';
import SourcingPage from './pages/SourcingPage.jsx';
import ProductManagementPage from './pages/ProductManagementPage.jsx';
import OrdersAndCsPage from './pages/OrdersAndCsPage.jsx';
import MonitoringPage from './pages/MonitoringPage.jsx';
import SettingsPage from './pages/SettingsPage.jsx';

const NAV = [
  { id: 'dashboard',  href: '/',           label: '대시보드' },
  { id: 'discovery',  href: '/discovery',  label: '디스커버리' },
  { id: 'sourcing',   href: '/sourcing',   label: '소싱' },
  { id: 'products',   href: '/products',   label: '상품 관리' },
  { id: 'orders',     href: '/orders',     label: '주문·CS' },
  { id: 'monitor',    href: '/monitor',    label: '모니터링' },
  { id: 'settings',   href: '/settings',   label: '설정' },
];

export default function App() {
  const { user, loading, logout } = useAuth();
  const location = useLocation();

  const { data: summary } = useQuery({
    queryKey: ['hub', 'summary'],
    queryFn: () => apiFetch('/api/hub/summary'),
    enabled: !!user,
  });

  if (loading) return <div className="flex h-screen items-center justify-center text-ink-500">로딩 중...</div>;
  if (!user) {
    window.location.href = '/login?next=' + encodeURIComponent('/purchase/');
    return null;
  }

  const items = NAV.map((n) => ({
    ...n,
    active: n.href === '/' ? location.pathname === '/' : location.pathname.startsWith(n.href),
  }));

  return (
    <div className="min-h-screen bg-ink-50">
      <GlobalTopBar
        activeApp="purchase"
        summary={summary}
        user={user}
        onLogout={logout}
        onLogoClick={() => (window.location.href = '/')}
      />
      <div className="mx-auto flex max-w-[1600px]">
        <Sidebar
          theme="pa"
          items={items}
          onSelect={(id) => {
            const item = items.find((i) => i.id === id);
            if (item) window.history.pushState({}, '', '/purchase' + item.href);
            window.dispatchEvent(new PopStateEvent('popstate'));
          }}
        />
        <main className="flex-1 px-6 py-8">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/discovery" element={<DiscoveryPage />} />
            <Route path="/sourcing" element={<SourcingPage />} />
            <Route path="/products" element={<ProductManagementPage />} />
            <Route path="/orders" element={<OrdersAndCsPage />} />
            <Route path="/monitor" element={<MonitoringPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
