# user-service — Keycloak User Management BE

BE service đúng nghĩa, thay thế cho việc OCR service tự gọi Keycloak Admin API.

## Tại sao cần BE service này

| Vấn đề | Giải pháp |
|---|---|
| OCR service phải tự code gọi Keycloak REST API, tự resolve UUID, tự cache token | Service này đóng gói hết vào 1 nơi |
| F5/WAF chặn `/admin/realms/*` từ bên ngoài | Service chạy trong cluster, gọi Keycloak qua cluster DNS |
| Logic nghiệp vụ (validation, error mapping, idempotency) rải khắp | Tập trung trong services/, REST API cao cấp cho caller |
| Khó test (mock `_request()` của requests) | Test với SDK mock, không cần Keycloak thật |

## Stack

- **Node.js 20** + **TypeScript 5** + **Fastify 5**
- **@keycloak/keycloak-admin-client** (chính thức từ Keycloak team)
- **zod** cho input validation
- **pino** cho structured logging
- **vitest** cho tests

## Cấu trúc

```
user-service/
├── src/
│   ├── index.ts                Entry point
│   ├── server.ts               Fastify setup + error handler + audit log
│   ├── config.ts               Zod-validated env config
│   ├── keycloak.ts             KcAdminClient singleton + domain errors
│   ├── auth.ts                 X-Service-Token middleware
│   ├── services/
│   │   ├── user-service.ts     CRUD user, password, attributes, required actions
│   │   ├── role-service.ts     Client roles (assign/remove/list, auto-resolve UUID)
│   │   └── credential-service.ts  OTP reset, list/delete credentials
│   └── routes/
│       ├── users.ts            /users, /users/:id, /users/:id/password, …
│       ├── credentials.ts      /users/:id/credentials, /otp/reset
│       ├── roles.ts            /users/:id/roles (assign/remove/list)
│       └── health.ts           /healthz, /readyz
├── tests/
│   ├── users.test.ts           Vitest + mock kcClient
│   └── roles.test.ts
├── k8s/
│   ├── deployment.yaml         Secret + Deployment + Service
│   ├── networkpolicy.yaml      Restrict ingress/egress
│   └── ingress-patch.yaml      Path /api/v1/users-svc
├── dev-realm/
│   └── agribank-realm.json     Keycloak realm import cho dev compose
├── Dockerfile                  Multi-stage (dev / builder / runtime)
├── docker-compose.yml          Dev stack: user-service + Keycloak local
├── package.json
├── tsconfig.json
├── vitest.config.ts
├── .env.example
├── .dockerignore
└── README.md
```

## REST API

Tất cả endpoint yêu cầu header `X-Service-Token: <shared-secret>` (trừ `/healthz`, `/readyz`).

### Users

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/users` | `{username, email?, firstName?, lastName?, password?, temporary?, requiredActions?, enabled?, attributes?}` | `201 {id, username}` |
| `GET` | `/users/by-username/:username` | — | `{found, user?}` |
| `GET` | `/users/:id` | — | `UserRepresentation` |
| `PUT` | `/users/:id` | `{email?, firstName?, lastName?, enabled?, requiredActions?, attributes?}` | `204` |
| `PUT` | `/users/:id/password` | `{password, temporary?}` | `204` |
| `PUT` | `/users/:id/attributes` | `Record<string, string[]>` (merge, không ghi đè) | `204` |
| `PUT` | `/users/:id/required-actions` | `string[]` (replace) | `{requiredActions}` |
| `POST` | `/users/:id/required-actions` | `string[]` (merge) | `{requiredActions}` |

### Credentials

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/users/:id/credentials` | — | `{credentials: CredentialRepresentation[]}` |
| `DELETE` | `/users/:id/credentials/:credentialId` | — | `204` |
| `POST` | `/users/:id/otp/reset` | — | `{deleted: number}` |

### Roles (client roles)

| Method | Path | Body / Query | Response |
|---|---|---|---|
| `GET` | `/users/:id/roles?clientId=banca-app` | — | `{roles: RoleRepresentation[]}` |
| `POST` | `/users/:id/roles?clientId=...` | `string[]` (role names) | `{assigned, skipped}` |
| `DELETE` | `/users/:id/roles?clientId=...` | `string[]` (role names) | `{removed, skipped}` |

Caller chỉ cần biết **tên role** (`"banca-seller"`), service tự resolve UUID từ `clientId`.

## Mapping từ code Python cũ → endpoint mới

Giúp refactor `ocr-service/app/routers/users.py`:

| Python cũ (`KeycloakClient`) | Endpoint mới |
|---|---|
| `create_user(username, ...)` | `POST /users` |
| `find_user_by_username(u)` | `GET /users/by-username/{u}` |
| `reset_password(id, pwd, tmp)` | `PUT /users/{id}/password` |
| `get_credentials(id)` | `GET /users/{id}/credentials` |
| `delete_credential(id, cid)` | `DELETE /users/{id}/credentials/{cid}` |
| `reset_otp(id)` | `POST /users/{id}/otp/reset` |
| `set_required_actions(id, a)` | `PUT /users/{id}/required-actions` |
| `ensure_required_actions(id, a)` | `POST /users/{id}/required-actions` |
| `update_user_attributes(id, attrs)` | `PUT /users/{id}/attributes` |
| `get_user_client_roles(id, uuid, cid)` | `GET /users/{id}/roles?clientId=...` |
| `assign_client_roles_batch(id, uuid, roles)` | `POST /users/{id}/roles` |
| `remove_client_roles_batch(id, uuid, roles)` | `DELETE /users/{id}/roles` |

## Dev local

### Cách 1: Docker Compose (khuyến nghị)

Khởi đầy đủ stack (user-service + Keycloak local với realm `agribank` import sẵn):

```bash
cd user-service
cp .env.example .env

# Sửa .env nếu cần (mặc định đã có sẵn cho dev local)
#   SERVICE_API_KEY=dev-service-token-123
#   KEYCLOAK_CLIENT_SECRET=dev-secret-change-me

docker compose up --build
```

Sau khi các container healthy:

```bash
# Health check
curl http://localhost:8300/healthz
# → {"status":"ok"}

# Test 1 endpoint (cần header auth)
curl -i \
  -H "X-Service-Token: dev-service-token-123" \
  http://localhost:8300/users/by-username/alice
# → {"found":false,"username":"alice"}  (chưa có user)

# Truy cập Keycloak admin console
# http://localhost:8080  (admin / admin)
# → realm "agribank" đã import sẵn với client "ocr-banca-service" + "banca-app"
```

Hot reload: sửa file trong `src/` → container tự restart qua `tsx watch`.

```bash
# Xem log
docker compose logs -f user-service

# Dừng + xóa volume (reset Keycloak data)
docker compose down -v
```

### Cách 2: Chạy thẳng bằng Node (không Docker)

Cần có Keycloak chạy sẵn (local hoặc remote):

```bash
cd user-service
cp .env.example .env
# Sửa KEYCLOAK_INTERNAL_URL trỏ tới Keycloak của bạn
npm install
npm run dev          # tsx watch
```

### Cách 3: Chỉ dùng Keycloak local qua Docker

Nếu muốn dùng Keycloak local mà chạy user-service trực tiếp bằng Node:

```bash
# Start chỉ Keycloak
docker compose up -d keycloak

# Sau đó chạy user-service local
npm run dev
```

### Test

```bash
npm test                                    # Vitest, mock SDK
docker compose exec user-service npm test   # Test trong container
```

### Troubleshooting Docker dev

**Container không start / restart liên tục:**
```bash
docker compose logs user-service
# Thường do:
# - KEYCLOAK_CLIENT_SECRET sai → "invalid client credentials"
# - Keycloak chưa healthy khi user-service start (depends_on đã có healthcheck nhưng check logs)
```

**Port 8300 / 8080 đã bị chiếm:**
```bash
# Tìm process đang dùng
lsof -i :8300
lsof -i :8080

# Đổi port trong docker-compose.yml:
#   ports:
#     - "9300:8300"   # host:container
```

**Hot reload không hoạt động:**
```bash
# Kiểm tra volume mount
docker compose exec user-service ls -la /app/src
# Phải thấy file mới nhất (sửa trên host sẽ reflect trong container)

# Nếu không thấy → kiểm tra docker-compose.yml volumes section
# Nếu thấy rồi mà vẫn không reload → check tsx watch log
docker compose logs -f user-service
```

**Keycloak admin console không vào được:**
```bash
# Đợi ~60s sau khi container healthy (start-dev chậm)
docker compose logs keycloak | tail -20
# Phải thấy "Listening on: http://0.0.0.0:8080"
```

**Reset toàn bộ (xóa container + volume + image):**
```bash
docker compose down -v --rmi local
docker compose up --build
```

**Chạy shell trong container user-service:**
```bash
docker compose exec user-service sh
# Trong container:
#   ls src/
#   cat src/config.ts
#   npx vitest watch
```

## Deploy (Kubernetes / Production)

### 1. Tạo secret

```bash
kubectl -n agribank create secret generic user-service-secrets \
  --from-literal=SERVICE_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=KEYCLOAK_CLIENT_ID="ocr-banca-service" \
  --from-literal=KEYCLOAK_CLIENT_SECRET="<lấy từ Keycloak admin console>"
```

### 2. Apply manifests

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/ingress-patch.yaml
```

### 3. Test từ trong cluster

```bash
kubectl -n agribank exec -it deploy/user-service -- \
  wget -qO- http://localhost:8300/healthz
# → {"status":"ok"}

# Từ trong cluster, gọi qua ingress
SERVICE_TOKEN=$(kubectl -n agribank get secret user-service-secrets -o jsonpath='{.data.SERVICE_API_KEY}' | base64 -d)
curl -i -H "X-Service-Token: $SERVICE_TOKEN" \
  https://api.agribank.com.vn/api/v1/users-svc/users/by-username/alice
```

## Bảo mật

| Lớp | Cách làm |
|---|---|
| **Auth caller ↔ service** | Header `X-Service-Token` (256-bit random, K8s Secret, timing-safe compare) |
| **NetworkPolicy ingress** | Chỉ allow từ `ingress-nginx` namespace và pod `app=ocr-banca` |
| **NetworkPolicy egress** | Chỉ allow DNS + Keycloak pod |
| **TLS** | Dùng ingress có sẵn (cert đã có ở cluster) |
| **Path routing** | `/api/v1/users-svc` riêng biệt với `/api/v1/iam-bridge` (kc-proxy) |
| **runAsNonRoot** | UID 1000 (node user Alpine), readOnlyRootFilesystem, drop ALL caps |
| **Audit log** | Mỗi request ghi: rid, src IP, method, url, status, latency_ms |
| **Secret redaction** | Pino redact `authorization`, `x-service-token`, `x-proxy-key` |
| **Helmet** | Default security headers |
| **Validation** | Zod cho mọi input, fail-fast 400 |

## Lưu ý quan trọng

⚠️ **`KEYCLOAK_INTERNAL_URL` PHẢI là cluster DNS**, KHÔNG phải URL public. Set nhầm thì service vẫn bị F5 chặn.

⚠️ **Caller phải gửi `X-Service-Token` qua HTTPS ingress** (KHÔNG gửi qua HTTP). Token có thể lộ nếu đi plaintext.

⚠️ **`roles/:id/roles` endpoint sẽ cache UUID client trong memory 5 phút** (xem `CACHE_TTL_MS` trong `role-service.ts`). Nếu recreate client trong Keycloak, restart pod để clear cache.

⚠️ **Đừng gọi song song user-service và kc-proxy cùng lúc** — chỉ dùng 1 trong 2 để tránh race condition trên Keycloak (vd: 2 service cùng tạo user).

## Khi F5 được cấu hình

user-service vẫn giữ làm abstraction layer — chỉ đổi `KEYCLOAK_INTERNAL_URL` thành URL public nếu muốn gọi trực tiếp (bỏ qua cluster DNS). Hoặc để nguyên cluster DNS cho latency thấp hơn.

## Lộ trình migration từ kc-proxy sang user-service

1. **Giai đoạn 1 (đã xong):** `kc-proxy/` — bypass F5 với proxy mỏng
2. **Giai đoạn 2 (hiện tại):** `user-service/` — BE service thật, dùng SDK chính thức
3. **Giai đoạn 3:** Refactor OCR service — thay thế `KeycloakClient` bằng HTTP client gọi user-service
4. **Giai đoạn 4:** Decommission `kc-proxy/` (hoặc giữ làm fallback)