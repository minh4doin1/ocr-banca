/**
 * Batch user provisioning — enrich, review, provision Keycloak.
 * Phụ thuộc biến global từ app.js: API_BASE, jobId, setStep, stopPolling.
 */

let fieldConfig = null;
let enrichedUsers = [];
let batchReviewOpen = false;

async function loadFieldConfig() {
    try {
        const res = await fetch(`${API_BASE}/api/users/field-config`);
        if (res.ok) fieldConfig = await res.json();
    } catch {
        fieldConfig = { required_fields: ['username', 'name', 'cccd'], banca_core_enabled: false };
    }
}

function normalizeHeader(t) {
    return String(t || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

function headerFieldMap(table) {
    const map = {};
    if (!table?.cells?.length) return map;
    const headerCells = table.cells.filter(c => c.row === 0);
    const aliases = fieldConfig?.header_map || {};
    for (const cell of headerCells) {
        const norm = normalizeHeader(cell.text);
        for (const [field, list] of Object.entries(aliases)) {
            if ((list || []).includes(norm)) {
                map[cell.col] = field;
            }
        }
    }
    return map;
}

function validateCellByField(field, text, confidence, isHeader) {
    if (isHeader) return '';
    if (!text?.trim()) return 'Không được trống';
    if (field === 'cccd' && !/^\d{12}$/.test(text.replace(/\s+/g, ''))) {
        return 'CCCD phải có 12 số';
    }
    if ((field === 'name' || field === 'first_name') && text && /\d/.test(text)) {
        return 'Tên chứa chữ số';
    }
    if (confidence < 0.85) return 'Độ tin cậy thấp';
    return '';
}

async function enrichBatchUsers() {
    const res = await fetch(`${API_BASE}/api/users/enrich`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Enrich thất bại');
    return data;
}

function matchBadge(status) {
    const cls = { auto: 'badge-auto', suggest: 'badge-suggest', manual: 'badge-manual' };
    const label = { auto: 'Khớp tự động', suggest: 'Gợi ý', manual: 'Chọn thủ công' };
    return `<span class="match-badge ${cls[status] || 'badge-manual'}">${label[status] || status}</span>`;
}

async function searchAgencies(q) {
    const res = await fetch(`${API_BASE}/api/users/lookup/agencies?search=${encodeURIComponent(q)}&size=15`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
}

async function searchAgents(q, agencyId) {
    let url = `${API_BASE}/api/users/lookup/agents?search=${encodeURIComponent(q)}&size=15`;
    if (agencyId) url += `&agency_id=${encodeURIComponent(agencyId)}`;
    const res = await fetch(url);
    if (!res.ok) return [];
    const data = await res.json();
    return data.items || [];
}

function renderBatchReviewTable(users) {
    const tbody = document.getElementById('batch-review-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    users.forEach((u, idx) => {
        const tr = document.createElement('tr');
        const missing = (u.missing_fields || []).join(', ');
        tr.innerHTML = `
            <td>${escapeAttr(u.username)}</td>
            <td><input class="batch-inp" data-idx="${idx}" data-f="name" value="${escapeAttr(u.name || '')}"></td>
            <td><input class="batch-inp" data-idx="${idx}" data-f="cccd" value="${escapeAttr(u.cccd || '')}"></td>
            <td><input class="batch-inp" data-idx="${idx}" data-f="email" value="${escapeAttr(u.email || '')}"></td>
            <td><input class="batch-inp branch-name-inp" data-idx="${idx}" data-f="branch_name" value="${escapeAttr(u.branch_name || '')}"></td>
            <td>
                <div class="picker-wrap">
                    <input class="batch-inp branch-code-inp" data-idx="${idx}" data-f="branch_code" value="${escapeAttr(u.branch_code || '')}" placeholder="Mã CN">
                    <button type="button" class="btn btn-xs btn-ghost btn-pick-branch" data-idx="${idx}">Tìm</button>
                </div>
            </td>
            <td>
                <div class="picker-wrap">
                    <input class="batch-inp agent-code-inp" data-idx="${idx}" data-f="agent_code" value="${escapeAttr(u.agent_code || '')}" placeholder="Mã ĐL">
                    <button type="button" class="btn btn-xs btn-ghost btn-pick-agent" data-idx="${idx}">Tìm</button>
                </div>
            </td>
            <td>${matchBadge(u.match_status)} ${u.match_confidence ? (u.match_confidence * 100).toFixed(0) + '%' : ''}</td>
            <td>
                <select class="batch-inp" data-idx="${idx}" data-f="on_conflict">
                    <option value="skip">Bỏ qua nếu trùng</option>
                    <option value="reset_password">Đổi MK</option>
                    <option value="reset_otp">Đổi OTP</option>
                    <option value="reset_both">Đổi cả 2</option>
                </select>
            </td>
            <td class="${missing ? 'cell-err' : ''}">${missing || 'OK'}</td>`;
        tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.batch-inp').forEach(el => {
        el.addEventListener('change', () => {
            const i = +el.dataset.idx;
            const f = el.dataset.f;
            if (f === 'on_conflict') enrichedUsers[i].on_conflict = el.value;
            else enrichedUsers[i][f] = el.value;
        });
    });

    tbody.querySelectorAll('.btn-pick-branch').forEach(btn => {
        btn.addEventListener('click', () => openAgencyPicker(+btn.dataset.idx));
    });
    tbody.querySelectorAll('.btn-pick-agent').forEach(btn => {
        btn.addEventListener('click', () => openAgentPicker(+btn.dataset.idx));
    });
}

async function openAgencyPicker(idx) {
    const q = prompt('Tìm chi nhánh (tên hoặc mã):', enrichedUsers[idx].branch_name || '');
    if (q === null) return;
    const items = await searchAgencies(q);
    if (!items.length) { alert('Không tìm thấy chi nhánh.'); return; }
    const list = items.map((a, i) => `${i + 1}. ${a.core_bank_code} — ${a.name}`).join('\n');
    const pick = prompt(`Chọn số (1-${items.length}):\n${list}`, '1');
    const n = parseInt(pick, 10);
    if (n >= 1 && n <= items.length) {
        enrichedUsers[idx].branch_code = items[n - 1].core_bank_code;
        enrichedUsers[idx].agent_code = enrichedUsers[idx].agent_code || items[n - 1].agency_code;
        enrichedUsers[idx].branch_name_matched = items[n - 1].name;
        enrichedUsers[idx].match_status = 'manual';
        renderBatchReviewTable(enrichedUsers);
    }
}

async function openAgentPicker(idx) {
    const q = prompt('Tìm đại lý (tên/email/mã):', enrichedUsers[idx].email || '');
    if (q === null) return;
    const items = await searchAgents(q, enrichedUsers[idx].agency_id || '');
    if (!items.length) { alert('Không tìm thấy đại lý.'); return; }
    const list = items.map((a, i) => `${i + 1}. ${a.agent_code || '—'} — ${a.name} (${a.email})`).join('\n');
    const pick = prompt(`Chọn số (1-${items.length}):\n${list}`, '1');
    const n = parseInt(pick, 10);
    if (n >= 1 && n <= items.length) {
        enrichedUsers[idx].agent_code = items[n - 1].agent_code;
        if (items[n - 1].branch_code) enrichedUsers[idx].branch_code = items[n - 1].branch_code;
        enrichedUsers[idx].match_status = 'manual';
        renderBatchReviewTable(enrichedUsers);
    }
}

function showBatchReviewModal(users) {
    enrichedUsers = users.map(u => ({ ...u, on_conflict: u.on_conflict || 'skip' }));
    const modal = document.getElementById('batch-review-modal');
    if (!modal) return;
    renderBatchReviewTable(enrichedUsers);
    modal.classList.remove('hidden');
    batchReviewOpen = true;
}

function hideBatchReviewModal() {
    document.getElementById('batch-review-modal')?.classList.add('hidden');
    batchReviewOpen = false;
}

async function confirmBatchProvision() {
    const defaultConflict = document.getElementById('batch-default-conflict')?.value || 'skip';
    const btn = document.getElementById('btn-batch-confirm');
    if (btn) { btn.disabled = true; btn.textContent = 'Đang tạo lô...'; }

    try {
        const res = await fetch(`${API_BASE}/api/users/provision-batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                users: enrichedUsers,
                default_on_conflict: defaultConflict,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Tạo lô thất bại');

        hideBatchReviewModal();
        showBatchResults(data);
    } catch (e) {
        alert(`Lỗi: ${e.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Xác nhận tạo lô'; }
    }
}

function showBatchResults(data) {
    successBatchCode.textContent = `BATCH-${jobId.toUpperCase()}`;
    successTotalRecords.textContent = `${data.created} tạo mới · ${data.updated} cập nhật · ${data.skipped} bỏ qua · ${data.failed} lỗi`;

    const detail = document.getElementById('batch-results-detail');
    if (detail) {
        detail.innerHTML = (data.results || []).map(r => {
            const cls = r.status === 'failed' ? 'text-err' : r.status === 'created' ? 'text-ok' : '';
            return `<div class="result-row ${cls}"><strong>${escapeAttr(r.username)}</strong> — ${r.status}${r.error ? ': ' + escapeAttr(r.error) : ''}</div>`;
        }).join('');
    }
    stopPolling();
    setStep(3);
}

async function submitBatch() {
    if (!jobId) { alert('Chưa có job OCR.'); return; }
    const btn = btnConfirmBatch;
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Đang chuẩn bị...'; }

    try {
        const data = await enrichBatchUsers();
        if (!data.users?.length) throw new Error('Không có user để tạo lô.');
        showBatchReviewModal(data.users);
    } catch (e) {
        alert(`Lỗi: ${e.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
}

function setupBatchReviewModal() {
    document.getElementById('btn-batch-cancel')?.addEventListener('click', hideBatchReviewModal);
    document.getElementById('btn-batch-cancel-2')?.addEventListener('click', hideBatchReviewModal);
    document.getElementById('btn-batch-confirm')?.addEventListener('click', confirmBatchProvision);
    document.getElementById('btn-batch-re-enrich')?.addEventListener('click', async () => {
        try {
            const data = await enrichBatchUsers();
            enrichedUsers = data.users;
            renderBatchReviewTable(enrichedUsers);
        } catch (e) { alert(e.message); }
    });
}
