# kc-proxy — Keycloak Admin Proxy

Proxy trung gian giúp OCR service gọi Keycloak Admin API mà **không qua F5/WAF**.

## Vấn đề

```
OCR service → admin-sso.agribank.com/admin/realms/agribank/users
              └── F5 BIG-IP ASM: "Request Rejected" (HTML, Support ID: 1810963468…)
```

F5 chưa cấu hình allow rule cho `/admin/*` → mọi request tới Keycloak admin API đều bị reject ở edge, **kể cả từ browser** (xác nhận qua screenshot ngày …). Token endpoint (`/realms/.../protocol/openid-connect/*`) hoạt động vì đã được allow rule thủ công trước đó.

## Giải pháp

Deploy 1 service FastAPI nhỏ (≤ 100 dòng) **vào cùng Kubernetes cluster với Keycloak**. Traffic proxy → Keycloak đi qua **cluster DNS nội bộ** (`http://keycloak.keycloak.svc.cluster.local:8080`), hoàn toàn không qua F5.

```
┌──────────────┐    HTTPS    ┌─────────────────┐   HTTP nội bộ   ┌──────────────┐
│  OCR service │ ──────────► │  kc-proxy       │ ──────────────► │  Keycloak    │
│  (LAN/wifi)  │  ingress    │  (Pod trong     │  cluster.local  │  (Pod)       │
│              │             │   cluster)      │  :8080          │              │
└──────────────┘             └─────────────────┘                 └──────────────┘
                                     │
                                     ▼
                              F5 chỉ thấy request
                              ingress → proxy (LAN → ingress, OK)
                              proxy → Keycloak (cluster-internal, không qua F5)
```

## Cấu trúc

```
kc-proxy/
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI entrypoint + catch-all forward
│   ├── config.py           # pydantic settings
│   ├── auth.py             # X-Proxy-Key validation
│   ├── keycloak_client.py  # Token cache + forward logic
│   └── audit.py            # Audit log + Timer
├── tests/
│   └── test_proxy.py       # Unit tests (httpx mock)
├── k8s/
│   ├── deployment.yaml     # Namespace + Secret + Deployment + Service
│   ├── networkpolicy.yaml  # Restrict ingress/egress
│   └── ingress-patch.yaml  # Path lách
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Cách deploy

### 1. Tạo secret

```bash
kubectl -n agribank create secret generic kc-proxy-secrets \
  --from-literal=PROXY_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=KEYCLOAK_CLIENT_ID="ocr-banca-service" \
  --from-literal=KEYCLOAK_CLIENT_SECRET="<lấy từ Keycloak admin console>"
```

### 2. Apply manifests

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/ingress-patch.yaml
```

### 3. Cấu hình OCR service

```bash
# .env của ocr-service
KEYCLOAK_BASE_URL=https://api.agribank.com.vn/api/v1/iam-bridge
KEYCLOAK_PROXY_API_KEY=<cùng giá trị PROXY_API_KEY đã tạo ở bước 1>
```

### 4. Test từ trong cluster

```bash
kubectl -n agribank exec -it deploy/kc-proxy -- \
  curl -i -H "X-Proxy-Key: $PROXY_API_KEY" \
  http://localhost:8200/api/v1/iam-bridge/clients?clientId=banca-app
# Phải trả JSON (không phải HTML "Request Rejected")
```

## Bảo mật

| Lớp | Cách làm |
|---|---|
| **Auth OCR ↔ proxy** | Header `X-Proxy-Key` (256-bit random, K8s Secret) |
| **NetworkPolicy ingress** | Chỉ allow từ `ingress-nginx` namespace và pod `app=ocr-banca` |
| **NetworkPolicy egress** | Chỉ allow DNS + Keycloak pod (label `app=keycloak`) |
| **TLS** | Dùng ingress có sẵn (cert đã có ở cluster) |
| **Path lách** | `/api/v1/iam-bridge` — KHÔNG dùng tên hiển nhiên |
| **Ẩn docs** | `docs_url=None, redoc_url=None, openapi_url=None` |
| **runAsNonRoot** | UID 10001, readOnlyRootFilesystem, drop ALL caps |
| **Audit log** | Mỗi request ghi: rid, source_ip, method, path, status, latency_ms |

## Lưu ý quan trọng

⚠️ **`KEYCLOAK_INTERNAL_URL` PHẢI là cluster DNS**, KHÔNG phải URL public (`admin-sso.agribank.com`). Nếu set nhầm thành URL public thì proxy vẫn bị F5 chặn y như cũ.

⚠️ **Phải xác minh pod-to-pod traffic không qua F5**. Nếu cluster có Calico/Cilium với egress firewall, chạy thử:
```bash
kubectl -n agribank exec deploy/kc-proxy -- \
  curl -i http://keycloak.keycloak.svc.cluster.local:8080/admin/realms/agribank/clients
# Phải trả JSON (hoặc 401 từ Keycloak), KHÔNG phải HTML "Request Rejected"
```

⚠️ **Path lách KHÔNG thay thế auth**. Dù path khó đoán, `X-Proxy-Key` vẫn bắt buộc.

## Roadmap khi F5 được cấu hình

Sau khi team F5 add allow rule cho `/admin/realms/agribank/*`:
- Set `KEYCLOAK_BASE_URL=https://admin-sso.agribank.com`
- Bỏ `KEYCLOAK_PROXY_API_KEY` (để trống)
- OCR service gọi trực tiếp Keycloak, không qua proxy
- `kc-proxy` vẫn giữ làm fallback hoặc decommission

## Run dev local

```bash
cd kc-proxy
cp .env.example .env
# Sửa KEYCLOAK_CLIENT_ID/SECRET trong .env
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8200
```

```bash
# Test
curl -i -H "X-Proxy-Key: test-key-123" \
  http://localhost:8200/api/v1/iam-bridge/clients?clientId=banca-app
```

## Test

```bash
cd kc-proxy
pytest tests/ -v
```