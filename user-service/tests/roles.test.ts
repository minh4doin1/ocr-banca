/**
 * Tests cho role routes.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../src/keycloak.js', async () => {
  const actual = await vi.importActual<typeof import('../src/keycloak.js')>('../src/keycloak.js');
  const mockKc = {
    users: {
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
  process.env.LOG_LEVEL = 'silent';
  process.env.NODE_ENV = 'test';
  const { config } = await import('../src/config.js');
  (config as any).SERVICE_API_KEY = SERVICE_TOKEN;
  (config as any).KEYCLOAK_CLIENT_ID = 'svc';
  (config as any).KEYCLOAK_CLIENT_SECRET = 'secret';
  return buildServer();
}

const auth = { 'x-service-token': SERVICE_TOKEN };

describe('role routes', () => {
  let app: FastifyInstance;

  beforeEach(async () => {
    vi.clearAllMocks();
    app = await buildTestServer();
    await app.ready();
  });

  afterEach(async () => {
    await app.close();
  });

  describe('POST /users/:id/roles', () => {
    it('resolves clientId → UUID, skips already-assigned roles', async () => {
      __mockKc.clients.find.mockResolvedValue([{ id: 'client-uuid' }]);
      __mockKc.users.listClientRoleMappings.mockResolvedValue([
        { id: 'r1', name: 'banca-seller' },
      ]);
      __mockKc.clients.findRole.mockResolvedValue({ id: 'r2', name: 'banca-admin' });
      __mockKc.users.addClientRoleMappings.mockResolvedValue(undefined);

      const r = await app.inject({
        method: 'POST',
        url: '/users/u1/roles',
        headers: auth,
        payload: ['banca-seller', 'banca-admin'],
      });

      expect(r.statusCode).toBe(200);
      expect(r.json()).toEqual({
        assigned: ['banca-admin'],
        skipped: ['banca-seller'],
      });
    });

    it('uses default ROLES_CLIENT_ID when no query param', async () => {
      __mockKc.clients.find.mockResolvedValue([{ id: 'cached-uuid' }]);
      __mockKc.users.listClientRoleMappings.mockResolvedValue([]);
      __mockKc.clients.findRole.mockResolvedValue({ id: 'r1', name: 'banca-seller' });
      __mockKc.users.addClientRoleMappings.mockResolvedValue(undefined);

      await app.inject({
        method: 'POST',
        url: '/users/u1/roles',
        headers: auth,
        payload: ['banca-seller'],
      });

      expect(__mockKc.clients.find).toHaveBeenCalledWith(
        expect.objectContaining({ clientId: 'banca-app' }),
      );
    });
  });

  describe('DELETE /users/:id/roles', () => {
    it('removes existing roles, skips missing ones', async () => {
      __mockKc.clients.find.mockResolvedValue([{ id: 'client-uuid' }]);
      __mockKc.users.listClientRoleMappings.mockResolvedValue([
        { id: 'r1', name: 'banca-seller' },
      ]);
      __mockKc.users.delClientRoleMappings.mockResolvedValue(undefined);

      const r = await app.inject({
        method: 'DELETE',
        url: '/users/u1/roles',
        headers: auth,
        payload: ['banca-seller', 'banca-missing'],
      });

      expect(r.statusCode).toBe(200);
      expect(r.json()).toEqual({
        removed: ['banca-seller'],
        skipped: ['banca-missing'],
      });
    });
  });

  describe('GET /users/:id/roles', () => {
    it('lists roles for default client', async () => {
      __mockKc.clients.find.mockResolvedValue([{ id: 'client-uuid' }]);
      __mockKc.users.listClientRoleMappings.mockResolvedValue([
        { id: 'r1', name: 'banca-seller' },
      ]);

      const r = await app.inject({
        method: 'GET',
        url: '/users/u1/roles',
        headers: auth,
      });

      expect(r.statusCode).toBe(200);
      expect(r.json()).toEqual({
        roles: [{ id: 'r1', name: 'banca-seller' }],
      });
    });
  });
});