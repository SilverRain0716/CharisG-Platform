import React, { useEffect } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { ConsoleTopBar, ChannelTabs, AccountBar, Sidebar } from '@charisg/ui';
import { useAuth } from '@charisg/auth';

import { ChannelProvider, useChannel } from './channel.jsx';
import ConsoleHome from './pages/ConsoleHome.jsx';
import ChannelProductsPage from './pages/ChannelProductsPage.jsx';
import DiscoveryPage from './pages/DiscoveryPage.jsx';
import SourcingPage from './pages/SourcingPage.jsx';
import ProductManagementPage from './pages/ProductManagementPage.jsx';
import ProductSearchPage from './pages/ProductSearchPage.jsx';
import NewAccountUploadPage from './pages/NewAccountUploadPage.jsx';
import GroupsPage from './pages/GroupsPage.jsx';
import GroupDetailPage from './pages/GroupDetailPage.jsx';
import CategoryMappingPage from './pages/CategoryMappingPage.jsx';
import SettlementPage from './pages/SettlementPage.jsx';
import OrdersAndCsPage from './pages/OrdersAndCsPage.jsx';
import OrderDetailPage from './pages/OrderDetailPage.jsx';
import CsReturnsPage from './pages/CsReturnsPage.jsx';
import MonitoringPage from './pages/MonitoringPage.jsx';
import AutomationPage from './pages/AutomationPage.jsx';
import ScreeningPage from './pages/ScreeningPage.jsx';
import SettingsPage from './pages/SettingsPage.jsx';

/**
 * 메뉴 범위(scope)가 이 셸의 핵심 규칙이다.
 *   channel — 상단에서 고른 채널·계정의 데이터만 보여준다
 *   common  — 아마존 원천 데이터라 채널과 무관하다
 * 둘을 구분 없이 늘어놓으면 "채널을 바꾸면 소싱 목록도 바뀌나?" 하는 혼란이 생긴다.
 */
const NAV = [
  { id: 'home', href: '/', label: '대시보드', icon: <IconGrid /> },

  { type: 'group', id: 'g-channel', label: '채널 업무', scope: 'channel' },
  { id: 'ch-products', href: '/channel-products', label: '등록 상품', icon: <IconBox /> },
  { id: 'orders',      href: '/orders',      label: '주문', icon: <IconList />, deep: true },
  { id: 'cs-returns',  href: '/cs-returns',  label: 'CS·반품', icon: <IconChat /> },
  { id: 'settlement',  href: '/settlement',  label: '정산', icon: <IconWon /> },

  { type: 'group', id: 'g-common', label: '공통', scope: 'common' },
  { id: 'discovery',      href: '/discovery',      label: '디스커버리', icon: <IconSearch /> },
  { id: 'sourcing',       href: '/sourcing',       label: '소싱', icon: <IconChart /> },
  { id: 'products',       href: '/products',       label: '상품 마스터', icon: <IconDb /> },
  { id: 'product-search', href: '/product-search', label: '상품 검색', icon: <IconSearch /> },
  // 옵션 그룹·카테고리 매핑은 화면이 있는데 라우팅이 빠져 있었다(링크만 살아 있었음).
  { id: 'groups',         href: '/groups',         label: '옵션 그룹', icon: <IconBox />, deep: true },
  { id: 'category-map',   href: '/category-mapping', label: '카테고리 매핑', icon: <IconDb /> },
  { id: 'screening',      href: '/screening',      label: '지식재산권 검수', icon: <IconShield /> },

  { type: 'group', id: 'g-sys', label: '시스템' },
  { id: 'automation', href: '/automation', label: '자동화', icon: <IconGear /> },
  { id: 'monitor',    href: '/monitor',    label: '모니터링', icon: <IconPulse /> },
  { id: 'settings',   href: '/settings',   label: '설정', icon: <IconGear /> },
];

export default function App() {
  const { user, loading, logout } = useAuth();

  useEffect(() => {
    if (!loading && !user) {
      window.location.replace('/login?next=' + encodeURIComponent('/purchase/'));
    }
  }, [loading, user]);

  if (loading || !user) {
    return <div className="flex h-screen items-center justify-center text-sm text-ink-500">로딩 중...</div>;
  }

  return (
    <ChannelProvider>
      <Console user={user} onLogout={logout} />
    </ChannelProvider>
  );
}

function Console({ user, onLogout }) {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    channels, channel, account, channelMeta, accounts, setChannel, setAccount, withScope,
  } = useChannel();

  // 컨텍스트 전용 메뉴 — 그 계정에서 의미 있는 작업만 나타난다.
  // 쿠팡 신계정 업로드 화면은 목록이 아니라 배치 작업이라 '등록 상품'과 분리한다.
  const contextual = channel === 'coupang' && account === 'new'
    ? [{ id: 'ch-upload', href: '/channel-upload', label: '업로드 배치', icon: <IconUpload /> }]
    : [];

  const items = NAV.flatMap((n) => (n.id === 'ch-products' ? [n, ...contextual] : [n]))
    .map((n) => ({
      ...n,
      active: n.type === 'group' ? false
        : n.href === '/' ? location.pathname === '/'
          : location.pathname === n.href || (n.deep && location.pathname.startsWith(n.href + '/')),
    }));

  const accountLabel = channel === 'all'
    ? '전체'
    : (accounts.find((a) => a.account_key === account)?.label || '');

  return (
    // data-channel 이 --accent 를 결정한다 — 탭·사이드바 활성·주 버튼이 한꺼번에 채널색을 따른다.
    // 어느 채널에 있는지가 주변시로도 인지돼야 엉뚱한 계정에 작업하는 사고가 준다.
    <div data-channel={channel} className="min-h-screen bg-canvas">
      {/* 두 줄을 통째로 고정한다 — 계정 줄이 스크롤에 밀려 사라지면
          "지금 어느 계정인가"를 놓치고, 그게 이 콘솔에서 제일 위험한 사고다. */}
      <div className="sticky top-0 z-50 bg-surface">
        <ConsoleTopBar user={user} onLogout={onLogout} onLogoClick={() => navigate(withScope('/'))}>
          <ChannelTabs
            channels={channels}
            value={channel}
            onChange={setChannel}
            onBlocked={() => navigate(withScope('/settings'))}
          />
        </ConsoleTopBar>

        {/* 2행 — 계정 줄. 전체 탭은 채널이 여럿이라 계정 선택이 성립하지 않는다. */}
        {channel !== 'all' ? (
          <AccountBar
            channel={channel}
            label={channelMeta.label}
            mark={channelMeta.mark}
            accounts={accounts}
            value={account}
            onChange={setAccount}
          />
        ) : (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-sunken px-3 py-2 text-[12.5px] text-ink-500">
            <span className="font-semibold text-ink-900">전체 채널</span>
            <span>모든 채널·계정 합산. 특정 계정에 작업하려면 위에서 채널을 고르세요.</span>
          </div>
        )}
      </div>

      <div className="mx-auto flex max-w-[1600px]">
        <Sidebar
          items={items}
          scopeLabel={channel === 'all' ? '전체' : `${channelMeta.label}${accountLabel ? ' · ' + accountLabel : ''}`}
          onSelect={(id) => {
            const item = items.find((i) => i.id === id);
            if (item) navigate(withScope(item.href));
          }}
        />
        <main className="min-w-0 flex-1 p-3">
          <Routes>
            <Route path="/" element={<ConsoleHome />} />
            <Route path="/channel-products" element={<ChannelProductsPage />} />
            <Route path="/channel-upload" element={<NewAccountUploadPage />} />
            <Route path="/orders" element={<OrdersAndCsPage />} />
            <Route path="/orders/:id" element={<OrderDetailPage />} />
            <Route path="/cs-returns" element={<CsReturnsPage />} />
            <Route path="/settlement" element={<SettlementPage />} />
            <Route path="/discovery" element={<DiscoveryPage />} />
            <Route path="/sourcing" element={<SourcingPage />} />
            <Route path="/products" element={<ProductManagementPage />} />
            <Route path="/product-search" element={<ProductSearchPage />} />
            <Route path="/groups" element={<GroupsPage />} />
            <Route path="/groups/:asin" element={<GroupDetailPage />} />
            <Route path="/category-mapping" element={<CategoryMappingPage />} />
            <Route path="/screening" element={<ScreeningPage />} />
            <Route path="/automation" element={<AutomationPage />} />
            <Route path="/monitor" element={<MonitoringPage />} />
            <Route path="/settings" element={<SettingsPage />} />

            {/* 옛 채널별 경로 — 채널 탭으로 흡수됐다. 북마크가 죽지 않게 넘겨준다. */}
            <Route path="/coupang" element={<Navigate to="/channel-products?ch=coupang&acct=old" replace />} />
            <Route path="/new-account-upload" element={<Navigate to="/channel-products?ch=coupang&acct=new" replace />} />
            <Route path="/smartstore" element={<Navigate to="/channel-products?ch=smartstore&acct=old" replace />} />
            <Route path="/smartstore/new" element={<Navigate to="/channel-products?ch=smartstore&acct=new" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

/* ── 아이콘 ────────────────────────────────────────────── */
const ico = { width: 14, height: 14, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2 };
function IconGrid()   { return <svg {...ico}><rect x="3" y="3" width="7" height="9" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="12" width="7" height="9" /><rect x="3" y="16" width="7" height="5" /></svg>; }
function IconBox()    { return <svg {...ico}><path d="M20 7 12 3 4 7v10l8 4 8-4z" /><path d="M4 7l8 4 8-4M12 21V11" /></svg>; }
function IconList()   { return <svg {...ico}><path d="M3 6h18M6 12h12M10 18h4" /></svg>; }
function IconChat()   { return <svg {...ico}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>; }
function IconWon()    { return <svg {...ico}><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>; }
function IconSearch() { return <svg {...ico}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>; }
function IconChart()  { return <svg {...ico}><path d="M3 3v18h18" /><path d="m7 14 4-4 3 3 5-6" /></svg>; }
function IconDb()     { return <svg {...ico}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18" /></svg>; }
function IconShield() { return <svg {...ico}><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /></svg>; }
function IconGear()   { return <svg {...ico}><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1" /></svg>; }
function IconUpload() { return <svg {...ico}><path d="M12 16V4M7 9l5-5 5 5" /><path d="M4 20h16" /></svg>; }
function IconPulse()  { return <svg {...ico}><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>; }
