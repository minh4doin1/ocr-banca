/**
 * Entry point.
 */

import { config } from './config.js';
import { buildServer } from './server.js';

async function main() {
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