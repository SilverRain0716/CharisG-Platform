import React, { useEffect } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from '@charisg/auth';
import LoginPage from './pages/LoginPage.jsx';

/**
 * Hub — 이제 로그인 관문만 담당한다.
 *
 * 드랍쉬핑을 접으면서 "사업을 고르는" 층이 사라졌고, 남은 것은 구매대행 콘솔
 * 하나뿐이다. 중간에 대시보드를 한 번 거치게 하면 매번 클릭 한 번이 낭비되므로,
 * 로그인 직후 곧바로 콘솔로 보낸다.
 *
 * ★루트(/)를 콘솔이 직접 차지하게 하려면 nginx alias 와 vite base 를 함께
 *   바꿔야 한다. 그건 배포와 묶인 작업이라, 지금은 이 앱을 얇은 관문으로 남겨
 *   nginx 를 건드리지 않고 같은 결과를 만든다.
 */
const CONSOLE_URL = '/purchase/';

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-ink-500">
        로딩 중...
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/login" element={user ? <ToConsole /> : <LoginPage />} />
      <Route path="/" element={user ? <ToConsole /> : <Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

/** 콘솔은 다른 번들이라 라우터로 못 간다 — 문서 이동으로 넘긴다. */
function ToConsole() {
  useEffect(() => {
    window.location.replace(CONSOLE_URL);
  }, []);
  return (
    <div className="flex h-screen items-center justify-center text-sm text-ink-500">
      콘솔로 이동 중...
    </div>
  );
}
