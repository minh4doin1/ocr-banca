/**
 * Routes: Credentials & OTP
 */

import type { FastifyInstance, FastifyPluginAsync } from 'fastify';
import { z } from 'zod';

import { requireServiceAuth } from '../auth.js';
import { UserNotFoundError } from '../keycloak.js';
import {
  deleteCredential,
  listCredentials,
  resetOtp,
} from '../services/credential-service.js';

const ParamsUserId = z.object({ id: z.string().min(1) });
const ParamsCredId = z.object({ id: z.string().min(1), credentialId: z.string().min(1) });

export const credentialRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  app.addHook('preHandler', requireServiceAuth);

  // ── GET /users/:id/credentials ──
  app.get('/users/:id/credentials', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    try {
      const creds = await listCredentials(id);
      return { credentials: creds };
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── DELETE /users/:id/credentials/:credentialId ──
  app.delete('/users/:id/credentials/:credentialId', async (req, reply) => {
    const { id, credentialId } = ParamsCredId.parse(req.params);
    try {
      await deleteCredential(id, credentialId);
      return reply.code(204).send();
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── POST /users/:id/otp/reset — xóa OTP credentials + gán CONFIGURE_TOTP ──
  app.post('/users/:id/otp/reset', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    try {
      const result = await resetOtp(id);
      return reply.send(result);
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });
};