import { beforeEach, describe, expect, it, vi } from "vitest";

import { setAccessToken } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

vi.mock("@/api/services", () => ({
  authApi: {
    login: vi.fn(),
    refresh: vi.fn(),
    logout: vi.fn(),
    me: vi.fn(),
  },
}));

import { authApi } from "@/api/services";

const mockedAuthApi = vi.mocked(authApi);

const user = {
  id: "user-1",
  username: "alice",
  displayName: "Alice",
  role: "ADMIN" as const,
  isActive: true,
  lastLoginAt: null,
  createdAt: "2030-01-01T12:00:00Z",
  updatedAt: "2030-01-01T12:00:00Z",
};

describe("authStore", () => {
  beforeEach(() => {
    useAuthStore.getState().reset();
    setAccessToken(null);
    vi.clearAllMocks();
  });

  it("test_login_sets_memory_token", async () => {
    mockedAuthApi.login.mockResolvedValue({
      accessToken: "memory-access-token",
      expiresIn: 900,
      user,
    });

    await useAuthStore.getState().login("alice", "correct-password");

    expect(useAuthStore.getState().accessToken).toBe("memory-access-token");
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(useAuthStore.getState().user?.username).toBe("alice");
    expect(globalThis).not.toHaveProperty("localStorage");
  });

  it("test_refresh_on_reload", async () => {
    mockedAuthApi.refresh.mockResolvedValue({
      accessToken: "restored-access-token",
      expiresIn: 900,
      user,
    });

    await useAuthStore.getState().bootstrap();

    expect(useAuthStore.getState().accessToken).toBe("restored-access-token");
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(useAuthStore.getState().user?.id).toBe("user-1");
  });
});