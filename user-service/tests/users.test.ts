/**
 * Tests cho user routes — mock kcClient để không cần Keycloak thật.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ── Mock Keycloak SDK trước khi import server ──
vi.mock('../src/keycloak.js', async () => {
  const actual = await vi.importActual<typeof import('../src/keycloak.js')>('../src/keycloak.js');
  const mockKc = {
    users: {
      create: vi.fn(),
      find: vi.fn(),
      findOne: vi.fn(),
      update: vi.fn(),
      resetPassword: vi.fn(),
      getCredentials: vi.fn(),
      deleteCredential: vi.fn(),
      listClientRoleMappings: vi.fn(),
      addClientRoleMappings: vi.fn(),
      delClientRoleMappings: vi.fn(),
    },
    clients: {
      find: vi.fn(),
      findRole: vi.fn(),
    },
    auth: vi.fn(),
  };

  return {
    ...actual,
    kcClient: {
      ensureAuth: vi.fn(),
      raw: () => mockKc,
    },
    // Expose mock để test có thể truy cập
    __mockKc: mockKc,
  };
});

import { buildServer } from '../src/server.js';
import { __mockKc } from '../src/keycloak.js';

const SERVICE_TOKEN = 'test-service-token';

async function buildTestServer(): Promise<FastifyInstance> {
  process.env.SERVICE_API_KEY = SERVICE_TOKEN;
  process.env.KEYCLOAK_CLIENT_ID = 'svc';
  process.env.KEYCLOAK_CLIENT_SECRET = 'secret';
  process.env.KEYCLOAK_INTERNAL_URL = 'http://kc.test:8080';
  process.env.KEYCLOAK_REALM = 'agribank';
  process.env.LOG_LEVEL = 'silent';
  process.env.NODE_ENV = 'test';
  // Reload config (cache từ lần load đầu)
  const { config } = await import('../src/config.js');
  (config as any).SERVICE_API_KEY = SERVICE_TOKEN;
  (config as any).KEYCLOAK_CLIENT_ID = 'svc';
  (config as any).KEYCLOAK_CLIENT_SECRET = 'secret';
  return buildServer();
}

const auth = { 'x-service-token': SERVICE_TOKEN };

describe('user routes', () => {
  let app: FastifyInstance;

  beforeEach(async () => {
    vi.clearAllMocks();
    app = await buildTestServer();
    await app.ready();
  });

  afterEach(async () => {
    await app.close();
  });

  // ── Auth ──

  describe('auth', () => {
    it('rejects missing token', async () => {
      const r = await app.inject({ method: 'POST', url: '/users', payload: { username: 'a' } });
      expect(r.statusCode).toBe(401);
    });

    it('rejects invalid token', async () => {
      const r = await app.inject({
        method: 'POST',
        url: '/users',
        headers: { 'x-service-token': 'wrong' },
        payload: { username: 'a' },
      });
      expect(r.statusCode).toBe(401);
    });

    it('accepts valid token', async () => {
      __mockKc.users.create.mockResolvedValue({ id: 'new-uuid' });
      const r = await app.inject({
        method: 'POST',
        url: '/users',
        headers: auth,
        payload: { username: 'alice' },
      });
      expect(r.statusCode).toBe(201);
    });
  });

  // ── POST /users ──

  describe('POST /users', () => {
    it('creates user with required fields only', async () => {
      __mockKc.users.create.mockResolvedValue({ id: 'new-uuid' });

      const r = await app.inject({
        method: 'POST',
        url: '/users',
        headers: auth,
        payload: { username: 'alice' },
      });

      expect(r.statusCode).toBe(201);
      expect(r.json()).toEqual({ id: 'new-uuid', username: 'alice' });
      expect(__mockKc.users.create).toHaveBeenCalledWith({
        realm: 'agribank',
        user: expect.objectContaining({
          username: 'alice',
          enabled: true,
        }),
      });
    });

    it('maps 409 from Keycloak → 409 with error code', async () => {
      __mockKc.users.create.mockRejectedValue({
        response: { status: 409, data: { errorMessage: 'User exists' } },
      });

      const r = await app.inject({
        method: 'POST',
        url: '/users',
        headers: auth,
        payload: { username: 'alice' },
      });

      expect(r.statusCode).toBe(409);
      expect(r.json()).toMatchObject({ error: 'user_exists' });
    });

    it('validates input (missing username)', async () => {
      const r = await app.inject({
        method: 'POST',
        url: '/users',
        headers: auth,
        payload: { email: 'alice@example.com' },
      });

      expect(r.statusCode).toBe(400);
      expect(r.json()).toMatchObject({ error: 'validation_error' });
    });
  });

  // ── GET /users/by-username/:username ──

  describe('GET /users/by-username/:username', () => {
    it('returns found=false when no user', async () => {
      __mockKc.users.find.mockResolvedValue([]);
      const r = await app.inject({
        method: 'GET',
        url: '/users/by-username/nobody',
        headers: auth,
      });
      expect(r.statusCode).toBe(200);
      expect(r.json()).toEqual({ found: false, username: 'nobody' });
    });

    it('prefers case-insensitive match over first result', async () => {
      __mockKc.users.find.mockResolvedValue([
        { id: 'u1', username: 'Alice' },
        { id: 'u2', username: 'alice' },
      ]);
      const r = await app.inject({
        method: 'GET',
        url: '/users/by-username/alice',
        headers: auth,
      });
      expect(r.statusCode).toBe(200);
      expect(r.json().user.id).toBe('u2');
    });
  });

  // ── PUT /users/:id/password ──

  describe('PUT /users/:id/password', () => {
    it('resets password', async () => {
      __mockKc.users.resetPassword.mockResolvedValue(undefined);
      const r = await app.inject({
        method: 'PUT',
        url: '/users/u1/password',
        headers: auth,
        payload: { password: 'NewPass123!', temporary: true },
      });
      expect(r.statusCode).toBe(204);
      expect(__mockKc.users.resetPassword).toHaveBeenCalledWith({
        realm: 'agribank',
        id: 'u1',
        credential: { type: 'password', value: 'NewPass123!', temporary: true },
      });
    });

    it('rejects password < 8 chars', async () => {
      const r = await app.inject({
        method: 'PUT',
        url: '/users/u1/password',
        headers: auth,
        payload: { password: 'short' },
      });
      expect(r.statusCode).toBe(400);
    });
  });
});