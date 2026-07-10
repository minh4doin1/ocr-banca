/**
 * User service — domain logic cho Keycloak user CRUD.
 *
 * Tất cả operations đều wrap bằng `withKeycloakErrors()` để map
 * Keycloak HTTP errors sang domain errors (UserExistsError, UserNotFoundError, …).
 */

import type UserRepresentation from '@keycloak/keycloak-admin-client/lib/defs/userRepresentation.js';
import type CredentialRepresentation from '@keycloak/keycloak-admin-client/lib/defs/credentialRepresentation.js';

import { config } from '../config.js';
import { kcClient, withKeycloakErrors, UserNotFoundError } from '../keycloak.js';

// ── DTOs (Zod schemas cho input validation) ──

export interface CreateUserInput {
  username: string;
  email?: string;
  firstName?: string;
  lastName?: string;
  password?: string;
  temporary?: boolean;
  requiredActions?: string[];
  enabled?: boolean;
  attributes?: Record<string, string[]>;
}

export interface UpdateUserInput {
  email?: string;
  firstName?: string;
  lastName?: string;
  enabled?: boolean;
  requiredActions?: string[];
  attributes?: Record<string, string[]>;
}

// ── User CRUD ──

export async function createUser(input: CreateUserInput): Promise<{ id: string }> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const user: UserRepresentation = {
      username: input.username,
      enabled: input.enabled ?? true,
    };
    if (input.email) user.email = input.email;
    if (input.firstName) user.firstName = input.firstName;
    if (input.lastName) user.lastName = input.lastName;
    if (input.attributes) user.attributes = input.attributes;
    if (input.requiredActions) user.requiredActions = input.requiredActions;
    if (input.password) {
      user.credentials = [
        {
          type: 'password',
          value: input.password,
          temporary: input.temporary ?? true,
        },
      ];
    }

    const result = await kcClient.raw().users.create({ realm: config.KEYCLOAK_REALM, ...user });
    return { id: result.id };
  });
}

export async function findUserByUsername(username: string): Promise<UserRepresentation | null> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const users = await kcClient.raw().users.find({
      realm: config.KEYCLOAK_REALM,
      username,
      exact: true,
    });
    if (!Array.isArray(users) || users.length === 0) return null;
    // exact=true có thể vẫn trả nhiều record — ưu tiên match case-insensitive.
    const match = users.find(
      (u) => u.username?.toLowerCase() === username.toLowerCase(),
    );
    return match ?? users[0]!;
  });
}

export async function findUserById(userId: string): Promise<UserRepresentation> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const user = await kcClient.raw().users.findOne({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    });
    if (!user) throw new UserNotFoundError(userId);
    return user;
  });
}

export async function updateUser(userId: string, input: UpdateUserInput): Promise<void> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    // GET trước để giữ nguyên các field không update
    const existing = await kcClient.raw().users.findOne({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    });
    if (!existing) throw new UserNotFoundError(userId);

    const merged: UserRepresentation = {
      ...existing,
      ...(input.email !== undefined && { email: input.email }),
      ...(input.firstName !== undefined && { firstName: input.firstName }),
      ...(input.lastName !== undefined && { lastName: input.lastName }),
      ...(input.enabled !== undefined && { enabled: input.enabled }),
      ...(input.requiredActions !== undefined && {
        requiredActions: input.requiredActions,
      }),
      ...(input.attributes !== undefined && { attributes: input.attributes }),
    };

    await kcClient.raw().users.update(
      {
        realm: config.KEYCLOAK_REALM,
        id: userId,
      },
      merged,
    );
  });
}

export async function resetPassword(
  userId: string,
  password: string,
  temporary: boolean = true,
): Promise<void> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const credential: CredentialRepresentation = {
      type: 'password',
      value: password,
      temporary,
    };
    await kcClient.raw().users.resetPassword({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      credential,
    });
  });
}

// ── Attributes (merge thêm, không ghi đè) ──

export async function mergeUserAttributes(
  userId: string,
  attrs: Record<string, string[]>,
): Promise<void> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const existing = await kcClient.raw().users.findOne({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    });
    if (!existing) throw new UserNotFoundError(userId);

    const merged = { ...(existing.attributes ?? {}) };
    for (const [k, v] of Object.entries(attrs)) {
      if (v && v.length > 0) merged[k] = v;
    }

    await kcClient.raw().users.update(
      {
        realm: config.KEYCLOAK_REALM,
        id: userId,
      },
      {
        ...existing,
        attributes: merged,
      },
    );
  });
}

// ── Required Actions (merge, không ghi đè) ──

export async function ensureRequiredActions(
  userId: string,
  actions: string[],
): Promise<string[]> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const existing = await kcClient.raw().users.findOne({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    });
    if (!existing) throw new UserNotFoundError(userId);

    const current = existing.requiredActions ?? [];
    const merged = Array.from(new Set([...current, ...actions]));
    if (merged.length === current.length) return merged; // không thay đổi

    await kcClient.raw().users.update(
      {
        realm: config.KEYCLOAK_REALM,
        id: userId,
      },
      {
        ...existing,
        requiredActions: merged,
      },
    );
    return merged;
  });
}

export async function setRequiredActions(
  userId: string,
  actions: string[],
): Promise<string[]> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(async () => {
    const existing = await kcClient.raw().users.findOne({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    });
    if (!existing) throw new UserNotFoundError(userId);

    await kcClient.raw().users.update(
      {
        realm: config.KEYCLOAK_REALM,
        id: userId,
      },
      {
        ...existing,
        requiredActions: actions,
      },
    );
    return actions;
  });
}