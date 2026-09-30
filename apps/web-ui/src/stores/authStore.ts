/** C48 前端认证状态机：access token 只保存在内存。 */

import { create } from "zustand";

import { authApi } from "@/api/services";
import { setAccessToken, setRefreshHandler } from "@/api/client";
import type { components } from "@/api/generated/types";

type CurrentUser = components["schemas"]["CurrentUser"];
type Role = components["schemas"]["Role"];

export type AuthStatus = "idle" | "loading" | "authenticated" | "unauthenticated";

interface AuthState {
  accessToken: string | null;
  user: CurrentUser | null;
  status: AuthStatus;
  error: string | null;
  login: (username: string, password: string) => Promise<void>;
  refresh: () => Promise<string | null>;
  bootstrap: () => Promise<void>;
  logout: () => Promise<void>;
  me: () => Promise<void>;
  hasRole: (role: Role) => boolean;
  reset: () => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "认证失败";
}

export const useAuthStore = create<AuthState>((set, get) => {
  const clear = () => {
    setAccessToken(null);
    set({ accessToken: null, user: null, error: null, status: "unauthenticated" });
  };

  const applyToken = (data: components["schemas"]["TokenData"]) => {
    setAccessToken(data.accessToken);
    set({
      accessToken: data.accessToken,
      user: data.user,
      status: "authenticated",
      error: null,
    });
  };

  return {
    accessToken: null,
    user: null,
    status: "idle",
    error: null,

    async login(username: string, password: string): Promise<void> {
      set({ status: "loading", error: null });
      try {
        applyToken(await authApi.login({ username, password }));
      } catch (error) {
        clear();
        set({ error: errorMessage(error) });
        throw error;
      }
    },

    async refresh(): Promise<string | null> {
      try {
        const data = await authApi.refresh();
        applyToken(data);
        return data.accessToken;
      } catch {
        clear();
        return null;
      }
    },

    async bootstrap(): Promise<void> {
      if (get().status === "loading") return;
      set({ status: "loading", error: null });
      await get().refresh();
    },

    async logout(): Promise<void> {
      try {
        await authApi.logout();
      } finally {
        clear();
      }
    },

    async me(): Promise<void> {
      try {
        const user = await authApi.me();
        set({ user, status: "authenticated", error: null });
      } catch (error) {
        clear();
        set({ error: errorMessage(error) });
      }
    },

    hasRole(role: Role): boolean {
      return get().user?.role === role;
    },

    reset(): void {
      clear();
      set({ status: "idle" });
    },
  };
});

// 把 client 的 401 单飞 refresh 回调接到唯一 auth store。
setRefreshHandler(() => useAuthStore.getState().refresh());

export default useAuthStore;