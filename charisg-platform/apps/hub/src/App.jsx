import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from '@charisg/auth';
import LoginPage from './pages/LoginPage.jsx';
import HubDashboard from './pages/HubDashboard.jsx';

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-ink-500">
        로딩 중...
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
      <Route path="/" element={user ? <HubDashboard /> : <Navigate to="/login" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
