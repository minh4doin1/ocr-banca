/**
 * Entry point.
 */

import { config, ENV_FILE_PATH } from './config.js';
import { buildServer } from './server.js';

async function main() {
  console.info(
    `[user-service] config: env=${ENV_FILE_PATH} keycloak=${config.KEYCLOAK_INTERNAL_URL} realm=${config.KEYCLOAK_REALM} rolesClient=${config.ROLES_CLIENT_ID} kcClient=${config.KEYCLOAK_CLIENT_ID} auth=${config.SERVICE_API_KEY ? 'on' : 'off'}`,
  );

  const app = await buildServer();
  try {
    const address = await app.listen({ host: config.HOST, port: config.PORT });
    app.log.info(`user-service listening at ${address}`);
  } catch (err) {
    app.log.error({ err }, 'failed to start');
    process.exit(1);
  }

  for (const signal of ['SIGINT', 'SIGTERM'] as const) {
    process.on(signal, async () => {
      app.log.info({ signal }, 'shutting down');
      await app.close();
      process.exit(0);
    });
  }
}

main().catch((err) => {
  console.error('fatal startup error:', err);
  process.exit(1);
});