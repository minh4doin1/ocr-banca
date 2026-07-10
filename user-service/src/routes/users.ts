/**
 * Routes: User CRUD
 *
 * REST surface cao cấp — caller không cần biết Keycloak API.
 * Tất cả logic đã được wrap trong services.
 */

import type { FastifyInstance, FastifyPluginAsync } from 'fastify';
import { z } from 'zod';

import { requireServiceAuth } from '../auth.js';
import { UserExistsError, UserNotFoundError } from '../keycloak.js';
import {
  createUser,
  ensureRequiredActions,
  findUserById,
  findUserByUsername,
  mergeUserAttributes,
  resetPassword,
  setRequiredActions,
  updateUser,
} from '../services/user-service.js';

// ── Zod schemas ──

const CreateUserBody = z.object({
  username: z.string().min(1).max(255),
  email: z.string().email().optional(),
  firstName: z.string().max(255).optional(),
  lastName: z.string().max(255).optional(),
  password: z.string().min(8).optional(),
  temporary: z.boolean().optional().default(true),
  requiredActions: z.array(z.string()).optional(),
  enabled: z.boolean().optional().default(true),
  attributes: z.record(z.string(), z.array(z.string())).optional(),
});

const UpdateUserBody = z.object({
  email: z.string().email().optional(),
  firstName: z.string().max(255).optional(),
  lastName: z.string().max(255).optional(),
  enabled: z.boolean().optional(),
  requiredActions: z.array(z.string()).optional(),
  attributes: z.record(z.string(), z.array(z.string())).optional(),
});

const MergeAttributesBody = z.record(z.string(), z.array(z.string()));
const RequiredActionsBody = z.array(z.string());

const ParamsUsername = z.object({ username: z.string().min(1) });
const ParamsUserId = z.object({ id: z.string().min(1) });

const ResetPasswordBody = z.object({
  password: z.string().min(8),
  temporary: z.boolean().optional().default(true),
});

// ── Routes ──

export const userRoutes: FastifyPluginAsync = async (app: FastifyInstance) => {
  // Tất cả routes đều yêu cầu service token (ngoại trừ health)
  app.addHook('preHandler', requireServiceAuth);

  // ── POST /users — tạo user mới ──
  app.post('/users', async (req, reply) => {
    const body = CreateUserBody.parse(req.body);
    try {
      const { id } = await createUser(body);
      return reply.code(201).send({ id, username: body.username });
    } catch (err) {
      if (err instanceof UserExistsError) {
        return reply.code(409).send({ error: 'user_exists', message: err.message });
      }
      throw err;
    }
  });

  // ── GET /users/by-username/:username — tìm user theo username ──
  app.get('/users/by-username/:username', async (req) => {
    const { username } = ParamsUsername.parse(req.params);
    const user = await findUserByUsername(username);
    if (!user) {
      return { found: false, username };
    }
    return { found: true, user };
  });

  // ── GET /users/:id — lấy user theo UUID ──
  app.get('/users/:id', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    try {
      const user = await findUserById(id);
      return user;
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── PUT /users/:id — cập nhật user (email, firstName, lastName, enabled, requiredActions) ──
  app.put('/users/:id', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const body = UpdateUserBody.parse(req.body);
    try {
      await updateUser(id, body);
      return reply.code(204).send();
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── PUT /users/:id/password — reset password ──
  app.put('/users/:id/password', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const body = ResetPasswordBody.parse(req.body);
    try {
      await resetPassword(id, body.password, body.temporary);
      return reply.code(204).send();
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── PUT /users/:id/attributes — merge attributes (không ghi đè) ──
  app.put('/users/:id/attributes', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const body = MergeAttributesBody.parse(req.body);
    try {
      await mergeUserAttributes(id, body);
      return reply.code(204).send();
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── PUT /users/:id/required-actions (replace) ──
  app.put('/users/:id/required-actions', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const body = RequiredActionsBody.parse(req.body);
    try {
      const result = await setRequiredActions(id, body);
      return reply.send({ requiredActions: result });
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });

  // ── POST /users/:id/required-actions (merge, không ghi đè) ──
  app.post('/users/:id/required-actions', async (req, reply) => {
    const { id } = ParamsUserId.parse(req.params);
    const body = RequiredActionsBody.parse(req.body);
    try {
      const result = await ensureRequiredActions(id, body);
      return reply.send({ requiredActions: result });
    } catch (err) {
      if (err instanceof UserNotFoundError) {
        return reply.code(404).send({ error: 'user_not_found', id });
      }
      throw err;
    }
  });
};