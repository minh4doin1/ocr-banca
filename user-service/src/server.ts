/**
 * Fastify server setup.
 */

import Fastify, { type FastifyInstance } from 'fastify';
import fastifyHelmet from '@fastify/helmet';
import fastifySensible from '@fastify/sensible';

import { config } from './config.js';
import { KeycloakServiceError, UserExistsError, UserNotFoundError } from './keycloak.js';
import { credentialRoutes } from './routes/credentials.js';
import { healthRoutes } from './routes/health.js';
import { roleRoutes } from './routes/roles.js';
import { userRoutes } from './routes/users.js';

export async function buildServer(): Promise<FastifyInstance> {
  const app = Fastify({
    logger: {
      level: config.LOG_LEVEL,
      ...(config.NODE_ENV === 'development' && {
        transport: { target: 'pino-pretty', options: { colorize: true } },
      }),
      redact: {
        paths: [
          'req.headers.authorization',
          'req.headers["x-service-token"]',
          'req.headers["x-proxy-key"]',
        ],
        censor: '[REDACTED]',
      },
    },
    disableRequestLogging: false,
    trustProxy: true,
  });

  await app.register(fastifyHelmet, { contentSecurityPolicy: false });
  await app.register(fastifySensible);

  // ── Audit log mỗi request ──
  app.addHook('onResponse', async (req, reply) => {
    req.log.info(
      {
        rid: req.id,
        src: req.ip,
        method: req.method,
        url: req.url,
        status: reply.statusCode,
        latency_ms: reply.elapsedTime.toFixed(1),
      },
      'audit',
    );
  });

  // ── Error handler — map domain errors → HTTP ──
  app.setErrorHandler((err, _req, reply) => {
    if (err instanceof UserExistsError) {
      return reply.code(409).send({ error: 'user_exists', message: err.message });
    }
    if (err instanceof UserNotFoundError) {
      return reply.code(404).send({ error: 'user_not_found', message: err.message });
    }
    if (err instanceof KeycloakServiceError) {
      const status = err.status && err.status >= 400 && err.status < 600 ? err.status : 502;
      return reply
        .code(status)
        .send({ error: 'keycloak_error', message: err.message, upstream_status: err.status });
    }
    if (err.name === 'ZodError') {
      return reply.code(400).send({
        error: 'validation_error',
        issues: (err as any).issues,
      });
    }
    // Unknown error
    reply.log.error({ err }, 'unhandled error');
    return reply.code(500).send({ error: 'internal_error', message: err.message });
  });

  // ── Routes ──
  await app.register(healthRoutes);
  await app.register(userRoutes);
  await app.register(credentialRoutes);
  await app.register(roleRoutes);

  return app;
}