/**
 * Role service — gán / gỡ / liệt kê client roles cho user.
 *
 * Tự resolve UUID từ clientId + roleName — caller không cần biết UUID.
 * Cache UUID của client trong memory để giảm API call.
 */

import type { RoleRepresentation } from '@keycloak/keycloak-admin-client/lib/defs/roleRepresentation.js';

import { config } from '../config.js';
import { kcClient, withKeycloakErrors } from '../keycloak.js';

interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

const CACHE_TTL_MS = 5 * 60 * 1000; // 5 phút
const clientUuidCache = new Map<string, CacheEntry<string>>();

async function resolveClientUuid(clientId: string): Promise<string> {
  const cached = clientUuidCache.get(clientId);
  if (cached && cached.expiresAt > Date.now()) return cached.value;

  await kcClient.ensureAuth();
  const clients = await withKeycloakErrors(() =>
    kcClient.raw().clients.find({ realm: config.KEYCLOAK_REALM, clientId }),
  );
  const first = clients[0];
  if (!first?.id) {
    throw new Error(`Client '${clientId}' không tồn tại trong realm.`);
  }
  clientUuidCache.set(clientId, {
    value: first.id,
    expiresAt: Date.now() + CACHE_TTL_MS,
  });
  return first.id;
}

async function resolveRoleRepresentation(
  clientUuid: string,
  roleName: string,
): Promise<RoleRepresentation> {
  await kcClient.ensureAuth();
  const role = await withKeycloakErrors(() =>
    kcClient.raw().clients.findRole({
      realm: config.KEYCLOAK_REALM,
      id: clientUuid,
      roleName,
    }),
  );
  if (!role?.id || !role?.name) {
    throw new Error(`Role '${roleName}' không tồn tại trong client.`);
  }
  return role as RoleRepresentation;
}

export async function getUserClientRoles(
  userId: string,
  clientId: string = config.ROLES_CLIENT_ID,
): Promise<RoleRepresentation[]> {
  const clientUuid = await resolveClientUuid(clientId);
  await kcClient.ensureAuth();
  return withKeycloakErrors(() =>
    kcClient.raw().users.listClientRoleMappings({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      clientUniqueId: clientUuid,
    }),
  ) as Promise<RoleRepresentation[]>;
}

export async function assignClientRoles(
  userId: string,
  roleNames: string[],
  clientId: string = config.ROLES_CLIENT_ID,
): Promise<{ assigned: string[]; skipped: string[] }> {
  if (roleNames.length === 0) return { assigned: [], skipped: [] };

  const clientUuid = await resolveClientUuid(clientId);
  await kcClient.ensureAuth();

  return withKeycloakErrors(async () => {
    // Lấy role hiện có để skip
    const current = (await kcClient.raw().users.listClientRoleMappings({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      clientUniqueId: clientUuid,
    })) as RoleRepresentation[];
    const currentNames = new Set(current.map((r) => r.name));

    const toAssign: RoleRepresentation[] = [];
    const skipped: string[] = [];
    for (const name of roleNames) {
      if (currentNames.has(name)) {
        skipped.push(name);
        continue;
      }
      const role = await resolveRoleRepresentation(clientUuid, name);
      toAssign.push(role);
    }

    if (toAssign.length === 0) {
      return { assigned: [], skipped };
    }

    await kcClient.raw().users.addClientRoleMappings({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      clientUniqueId: clientUuid,
      roles: toAssign,
    });
    return { assigned: toAssign.map((r) => r.name!), skipped };
  });
}

export async function removeClientRoles(
  userId: string,
  roleNames: string[],
  clientId: string = config.ROLES_CLIENT_ID,
): Promise<{ removed: string[]; skipped: string[] }> {
  if (roleNames.length === 0) return { removed: [], skipped: [] };

  const clientUuid = await resolveClientUuid(clientId);
  await kcClient.ensureAuth();

  return withKeycloakErrors(async () => {
    const current = (await kcClient.raw().users.listClientRoleMappings({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      clientUniqueId: clientUuid,
    })) as RoleRepresentation[];
    const currentByName = new Map(current.map((r) => [r.name, r]));

    const toRemove: RoleRepresentation[] = [];
    const skipped: string[] = [];
    for (const name of roleNames) {
      const r = currentByName.get(name);
      if (!r) {
        skipped.push(name);
        continue;
      }
      toRemove.push(r);
    }

    if (toRemove.length === 0) return { removed: [], skipped };

    await kcClient.raw().users.delClientRoleMappings({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      clientUniqueId: clientUuid,
      roles: toRemove,
    });
    return { removed: toRemove.map((r) => r.name!), skipped };
  });
}