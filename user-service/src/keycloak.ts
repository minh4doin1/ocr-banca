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
    // SDK tự check expiry; gọi auth() là idempotent.
    if (this.authInFlight) return this.authInFlight;
    this.authInFlight = (async () => {
      try {
        await this.kc.auth({
          grantType: 'client_credentials',
          clientId: config.KEYCLOAK_CLIENT_ID,
          clientSecret: config.KEYCLOAK_CLIENT_SECRET,
        });
      } finally {
        // Reset để lần refresh sau chạy lại
        this.authInFlight = null;
      }
    })();
    return this.authInFlight;
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