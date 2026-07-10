/**
 * Health & readiness probes cho Kubernetes.
 */

import type { FastifyInstance, FastifyPluginAsync } from 'fastify';

import { config } from '../config.js';

export const healthRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  app.get('/healthz', async () => ({ status: 'ok' }));

  app.get('/readyz', async (_req, reply) => {
    if (!config.KEYCLOAK_CLIENT_ID || !config.KEYCLOAK_CLIENT_SECRET) {
      return reply.code(503).send({
        status: 'not-ready',
        reason: 'kc credentials missing',
      });
    }
    return { status: 'ready' };
  });
};