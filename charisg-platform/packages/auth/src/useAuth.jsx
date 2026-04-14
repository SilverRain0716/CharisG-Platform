import React, { createContext, useContext, useEffect, useState } from 'react';
import { apiFetch } from './apiFetch.js';

const AuthContext = createContext({
  user: null,
  loading: true,
  login: async () => {},
  logout: async () => {},
});

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (import.meta.env.VITE_BYPASS_AUTH === '1') {
        if (!cancelled) {
          setUser({ id: 0, username: 'dev', role: 'admin' });
          setLoading(false);
        }
        return;
      }
      try {
        // silent401: 비로그인 상태에서는 자동 리다이렉트하지 않고 user=null 처리만
        const me = await apiFetch('/api/hub/auth/me', { silent401: true });
        if (!cancelled) setUser(me);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  async function login(username, password) {
    const data = await apiFetch('/api/hub/auth/login', {
      method: 'POST',
      body: { username, password },
    });
    setUser(data.user);
    return data;
  }

  async function logout() {
    try { await apiFetch('/api/hub/auth/logout', { method: 'POST' }); } catch {}
    setUser(null);
    window.location.href = '/login';
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
