/**
 * Credential service — quản lý credentials (đặc biệt là OTP).
 */

import type CredentialRepresentation from '@keycloak/keycloak-admin-client/lib/defs/credentialRepresentation.js';

import { config } from '../config.js';
import { kcClient, withKeycloakErrors, UserNotFoundError } from '../keycloak.js';

const OTP_CREDENTIAL_TYPE = 'otp';
const REQUIRED_ACTION_CONFIGURE_TOTP = 'CONFIGURE_TOTP';

export async function listCredentials(userId: string): Promise<CredentialRepresentation[]> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(() =>
    kcClient.raw().users.getCredentials({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    }),
  ) as Promise<CredentialRepresentation[]>;
}

export async function deleteCredential(userId: string, credentialId: string): Promise<void> {
  await kcClient.ensureAuth();
  return withKeycloakErrors(() =>
    kcClient.raw().users.deleteCredential({
      realm: config.KEYCLOAK_REALM,
      id: userId,
      credentialId,
    }),
  );
}

/**
 * Reset OTP: xóa tất cả credential type=otp, rồi gán required action CONFIGURE_TOTP.
 * Trả về số credential đã xóa.
 */
export async function resetOtp(userId: string): Promise<{ deleted: number }> {
  await kcClient.ensureAuth();

  const credentials = (await withKeycloakErrors(() =>
    kcClient.raw().users.getCredentials({
      realm: config.KEYCLOAK_REALM,
      id: userId,
    }),
  )) as CredentialRepresentation[];

  let deleted = 0;
  for (const cred of credentials) {
    if ((cred.type ?? '').toLowerCase() === OTP_CREDENTIAL_TYPE && cred.id) {
      await withKeycloakErrors(() =>
        kcClient.raw().users.deleteCredential({
          realm: config.KEYCLOAK_REALM,
          id: userId,
          credentialId: cred.id!,
        }),
      );
      deleted++;
    }
  }

  // Đảm bảo user có required action CONFIGURE_TOTP
  const user = await kcClient.raw().users.findOne({
    realm: config.KEYCLOAK_REALM,
    id: userId,
  });
  if (!user) throw new UserNotFoundError(userId);

  const current = user.requiredActions ?? [];
  if (!current.includes(REQUIRED_ACTION_CONFIGURE_TOTP)) {
    await withKeycloakErrors(() =>
      kcClient
        .raw()
        .users.update(
          {
            realm: config.KEYCLOAK_REALM,
            id: userId,
          },
          { ...user, requiredActions: [...current, REQUIRED_ACTION_CONFIGURE_TOTP] },
        ),
    );
  }

  return { deleted };
}