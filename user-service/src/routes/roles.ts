/**
 * Routes: Client roles (gán / gỡ / liệt kê)
 *
 * Caller chỉ cần biết tên role (vd: "banca-seller"), service tự resolve UUID.
 */

import type { FastifyInstance, FastifyPluginAsync } from 'fastify';
import { z } from 'zod';

import { requireServiceAuth } from '../auth.js';
import { UserNotFoundError } from '../keycloak.js';
import {
  assignClientRoles,
  getUserClientRoles,
  removeClientRoles,
} from '../services/role-service.js';

const ParamsUserId = z.object({ id: z.string().min(1) });
const RoleNamesBody = z.array(z.string().min(1));
const ClientIdQuery = z.object({ clientId: z.string().optional() });

export const roleRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  app.addHook('preHandler', requireServiceAuth);

  // ── GET /users/:id/roles ──
  app.get('/users/:id/roles', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const { clientId } = ClientIdQuery.parse(req.query);
    try {
      const roles = await getUserClientRoles(id, clientId);
      return { roles };
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── POST /users/:id/roles — assign ──
  app.post('/users/:id/roles', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const { clientId } = ClientIdQuery.parse(req.query);
    const body = RoleNamesBody.parse(req.body);
    try {
      const result = await assignClientRoles(id, body, clientId);
      return reply.send(result);
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── DELETE /users/:id/roles — remove ──
  app.delete('/users/:id/roles', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const { clientId } = ClientIdQuery.parse(req.query);
    const body = RoleNamesBody.parse(req.body);
    try {
      const result = await removeClientRoles(id, body, clientId);
      return reply.send(result);
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });
};