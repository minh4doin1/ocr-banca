/**
 * Keycloak admin client — singleton + auto auth.
 *
 * Dùng @keycloak/keycloak-admin-client (chính thức từ Keycloak team).
 * SDK lo: token cache, refresh, retry một phần.
 */

import KcAdminClient from '@keycloak/keycloak-admin-client';

import { config } from './config.js';

export class KeycloakServiceError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'KeycloakServiceError';
  }
}

export class UserExistsError extends KeycloakServiceError {
  constructor(username: string) {
    super(`User '${username}' đã tồn tại.`);
    this.name = 'UserExistsError';
  }
}

export class UserNotFoundError extends KeycloakServiceError {
  public readonly id: string;
  constructor(id: string) {
    super(`User '${id}' không tồn tại.`);
    this.name = 'UserNotFoundError';
    this.id = id;
  }
}

class KeycloakClient {
  private kc: KcAdminClient;
  private authInFlight: Promise<void> | null = null;
  private accessTokenExpiry = 0;

  constructor() {
    this.kc = new KcAdminClient({
      baseUrl: config.KEYCLOAK_INTERNAL_URL,
      realmName: config.KEYCLOAK_REALM,
    });
  }

  /**
   * Đảm bảo đã có access token. Gọi auth() nếu chưa / token hết hạn.
   * Tránh race condition bằng cách share 1 promise cho concurrent callers.
   */
  async ensureAuth(): Promise<void> {
    const now = Date.now();
    if (this.accessTokenExpiry > now + 10_000) {
      return;
    }

    // SDK tự check expiry; gọi auth() là idempotent.
    if (this.authInFlight) return this.authInFlight;
    this.authInFlight = (async () => {
      try {
        try {
          await this.kc.auth({
            grantType: 'client_credentials',
            clientId: config.KEYCLOAK_CLIENT_ID,
            clientSecret: config.KEYCLOAK_CLIENT_SECRET,
          });
          // TTL fallback khi SDK auth thành công.
          this.accessTokenExpiry = Date.now() + 45_000;
        } catch (err) {
          // keycloak-admin-client v26 có bug decode refresh token với client_credentials.
          // Fallback: tự lấy access_token rồi set trực tiếp vào SDK client.
          const maybeMsg = err instanceof Error ? err.message : String(err);
          if (!maybeMsg.includes("reading 'split'")) {
            throw err;
          }
          await this.authWithClientCredentialsFallback();
        }
      } finally {
        // Reset để lần refresh sau chạy lại
        this.authInFlight = null;
      }
    })();
    return this.authInFlight;
  }

  private async authWithClientCredentialsFallback(): Promise<void> {
    const tokenUrl = `${config.KEYCLOAK_INTERNAL_URL.replace(/\/+$/, '')}/realms/${config.KEYCLOAK_REALM}/protocol/openid-connect/token`;
    const body = new URLSearchParams({
      grant_type: 'client_credentials',
      client_id: config.KEYCLOAK_CLIENT_ID,
      client_secret: config.KEYCLOAK_CLIENT_SECRET,
    });
    const resp = await fetch(tokenUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
      signal: AbortSignal.timeout(config.KEYCLOAK_TIMEOUT_MS),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new KeycloakServiceError(
        `Keycloak auth failed (${resp.status}): ${text.slice(0, 300)}`,
        resp.status,
      );
    }
    const data = (await resp.json()) as {
      access_token?: string;
      expires_in?: number;
    };
    if (!data.access_token) {
      throw new KeycloakServiceError('Keycloak auth failed: missing access_token');
    }
    this.kc.setAccessToken(data.access_token);
    const expiresInMs = Math.max(5, Number(data.expires_in ?? 60) - 10) * 1000;
    this.accessTokenExpiry = Date.now() + expiresInMs;
  }

  /** Expose raw SDK cho routes (qua wrapper typed). */
  raw(): KcAdminClient {
    return this.kc;
  }
}

export const kcClient = new KeycloakClient();

/**
 * Wrap một async operation: bắt lỗi Keycloak HTTP, map sang domain error.
 *
 * `cause` từ SDK có shape: { response: { status, data } }.
 */
export async function withKeycloakErrors<T>(op: () => Promise<T>): Promise<T> {
  try {
    return await op();
  } catch (err: any) {
    const status: number | undefined = err?.response?.status ?? err?.statusCode;
    const data: any = err?.response?.data ?? err?.data;
    const message = data?.errorMessage || err?.message || 'Keycloak error';

    if (status === 409) {
      throw new UserExistsError(data?.errorMessage ?? 'user exists');
    }
    if (status === 404 && data?.errorMessage?.includes('User not found')) {
      throw new UserNotFoundError(data?.errorMessage);
    }
    throw new KeycloakServiceError(message, status, err);
  }
}