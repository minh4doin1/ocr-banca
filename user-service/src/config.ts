/**
 * user-service — Configuration.
 *
 * Load từ environment variables (hoặc .env khi dev local).
 * Validate bằng zod để fail-fast khi thiếu config quan trọng.
 */

import path from 'node:path';
import { config as loadDotenv } from 'dotenv';
import { z } from 'zod';

// Luôn đọc .env từ thư mục user-service/, không phụ thuộc cwd khi start.
const envPath = path.resolve(__dirname, '..', '.env');
loadDotenv({ path: envPath });

export const ENV_FILE_PATH = envPath;

const ConfigSchema = z.object({
  // ── Server ──
  HOST: z.string().default('0.0.0.0'),
  PORT: z.coerce.number().int().positive().default(8300),
  LOG_LEVEL: z.enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace']).default('info'),
  NODE_ENV: z.enum(['development', 'production', 'test']).default('production'),

  // ── Keycloak (cluster-internal DNS) ──
  KEYCLOAK_INTERNAL_URL: z
    .string()
    .url()
    .default('http://keycloak.keycloak.svc.cluster.local:8080'),
  KEYCLOAK_REALM: z.string().default('agribank'),
  KEYCLOAK_CLIENT_ID: z.string().min(1, 'KEYCLOAK_CLIENT_ID is required'),
  KEYCLOAK_CLIENT_SECRET: z.string().min(1, 'KEYCLOAK_CLIENT_SECRET is required'),
  KEYCLOAK_VERIFY_SSL: z.coerce.boolean().default(false),
  KEYCLOAK_TIMEOUT_MS: z.coerce.number().int().positive().default(30_000),

  // ── Auth (caller ↔ user-service) ──
  // Shared secret. Empty = tắt auth (chỉ dùng cho dev/test).
  SERVICE_API_KEY: z.string().default(''),

  // ── Client chứa role nghiệp vụ ──
  // Service tự resolve UUID từ tên này — caller không cần biết UUID.
  ROLES_CLIENT_ID: z.string().default('banca'),
});

export type AppConfig = z.infer<typeof ConfigSchema>;

function loadConfig(): AppConfig {
  const parsed = ConfigSchema.safeParse(process.env);
  if (!parsed.success) {
    const issues = parsed.error.issues
      .map((i) => `  - ${i.path.join('.')}: ${i.message}`)
      .join('\n');
    throw new Error(`Invalid configuration:\n${issues}`);
  }
  return parsed.data;
}

export const config: AppConfig = loadConfig();