/**
 * Auth middleware — verify X-Service-Token header.
 *
 * Caller (OCR service, FE admin tool, …) phải gửi header với shared secret.
 * Constant-time compare để chống timing attack.
 */

import type { FastifyReply, FastifyRequest } from 'fastify';
import crypto from 'node:crypto';

import { config } from './config.js';

declare module 'fastify' {
  interface FastifyRequest {
    serviceAuth?: {
      authenticated: true;
    };
  }
}

export async function requireServiceAuth(
  req: FastifyRequest,
  reply: FastifyReply,
): Promise<void> {
  // Dev mode: tắt auth
  if (!config.SERVICE_API_KEY) return;

  const provided = req.headers['x-service-token'];
  const headerValue = Array.isArray(provided) ? provided[0] : provided;

  if (!headerValue) {
    return reply.code(401).send({
      error: 'unauthorized',
      message: 'Missing X-Service-Token header',
    });
  }

  const expected = config.SERVICE_API_KEY;
  const a = Buffer.from(headerValue, 'utf8');
  const b = Buffer.from(expected, 'utf8');
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return reply.code(401).send({
      error: 'unauthorized',
      message: 'Invalid service token',
    });
  }

  req.serviceAuth = { authenticated: true };
}