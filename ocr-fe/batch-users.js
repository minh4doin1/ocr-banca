/**
 * Batch user provisioning — enrich, review, provision Keycloak.
 * Phụ thuộc biến global từ app.js: API_BASE, jobId, setStep, stopPolling.
 */

const SSO_COL_FIELDS = {
    0: 'stt',
    1: 'name',
    2: 'department_name',
    3: 'ipcas_code',
    4: 'cccd',
    5: 'email',
    6: 'phone',
    7: 'role',
    8: 'unit_code',
};

let fieldConfig = null;
let enrichedUsers = [];
let batchReviewOpen = false;
let batchShowErrorsOnly = false;

async function loadFieldConfig() {
    try {
        const res = await fetch(`${API_BASE}/api/users/field-config`);
        if (res.ok) fieldConfig = await res.json();
    } catch {
        fieldConfig = {
            required_fields: ['email', 'first_name', 'last_name', 'branch_code', 'ipcas_code', 'cccd', 'phone', 'unit_code', 'role'],
            field_labels: {
                email: 'Email', first_name: 'Tên', last_name: 'Họ', branch_code: 'Mã CN',
                ipcas_code: 'IPCAS', cccd: 'CCCD', phone: 'SĐT', unit_code: 'Mã ĐV', role: 'Vai trò',
                department_name: 'Phòng/Đơn vị',
            },
            sso_columns: [],
            roles: [
                { value: 'banca-admin', label: 'Quản trị' },
                { value: 'banca-seller', label: 'Đại lý viên' },
                { value: 'banca-accounting-operator', label: 'Kế toán viên' },
                { value: 'banca-accounting-controller', label: 'Phê duyệt viên' },
            ],
            default_temp_password: 'Agribank@123',
            banca_core_enabled: false,
        };
    }
    const hint = document.getElementById('batch-default-pwd-hint');
    if (hint && fieldConfig?.default_temp_password) {
        hint.textContent = fieldConfig.default_temp_password;
    }
    const bulkRole = document.getElementById('bulk-role');
    if (bulkRole) {
        bulkRole.innerHTML = '<option value="">-- Chọn --</option>' + roleOptionsHtml('');
    }
}

function fieldLabel(field) {
    return fieldConfig?.field_labels?.[field] || field;
}

function normalizeHeader(t) {
    return String(t || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

function getTableColFieldMap(table) {
    if (table?.table_kind === 'sso_agribank' || table?.num_cols === 9) {
        const map = {};
        Object.entries(SSO_COL_FIELDS).forEach(([col, field]) => {
            if (+col < table.num_cols) map[+col] = field;
        });
        return map;
    }
    return headerFieldMap(table);
}

function getColumnHeaderLabel(table, col) {
    const fieldMap = getTableColFieldMap(table);
    const field = fieldMap[col];
    if (field && fieldConfig?.sso_columns?.length) {
        const sso = fieldConfig.sso_columns.find(c => c.field === field);
        if (sso) return sso.label;
    }
    if (field && fieldConfig?.field_labels?.[field]) {
        return fieldConfig.field_labels[field];
    }
    return `Cột ${col + 1}`;
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
    if (isHeader || field === 'stt') return '';
    if (!text?.trim()) {
        const label = fieldLabel(field);
        return label ? `Thiếu ${label}` : 'Không được trống';
    }
    if (field === 'cccd' && !/^\d{12}$/.test(text.replace(/\s+/g, ''))) {
        return 'CCCD phải có 12 số';
    }
    if (field === 'phone' && !/^0\d{8,10}$/.test(text.replace(/\s+/g, ''))) {
        return 'SĐT không hợp lệ';
    }
    if ((field === 'name' || field === 'first_name') && text && /\d/.test(text)) {
        return 'Tên chứa chữ số';
    }
    if (field === 'email' && text.includes('@') && !text.toLowerCase().endsWith('@agribank.com.vn')) {
        return 'Email phải thuộc @agribank.com.vn';
    }
    if (confidence < 0.85) return 'Độ tin cậy thấp';
    return '';
}

function validateUserClient(user) {
    const errors = {};
    const required = fieldConfig?.required_fields || [];
    required.forEach(field => {
        const val = String(user[field] || '').trim();
        if (!val) errors[field] = `Thiếu ${fieldLabel(field)}`;
    });
    if (user.cccd && !/^\d{12}$/.test(String(user.cccd).replace(/\s/g, ''))) {
        errors.cccd = 'CCCD phải có 12 số';
    }
    if (user.phone && !/^0\d{8,10}$/.test(String(user.phone).replace(/\s/g, ''))) {
        errors.phone = 'SĐT không hợp lệ';
    }
    const email = String(user.email || user.username || '');
    if (email && !email.toLowerCase().endsWith('@agribank.com.vn')) {
        errors.email = 'Email phải thuộc @agribank.com.vn';
    }
    if (user.role && fieldConfig?.roles?.length) {
        const valid = fieldConfig.roles.map(r => r.value);
        if (!valid.includes(user.role)) errors.role = 'Vai trò không hợp lệ';
    }
    return errors;
}

function roleOptionsHtml(selected) {
    const roles = fieldConfig?.roles || [];
    return roles.map(r =>
        `<option value="${escapeAttr(r.value)}"${r.value === selected ? ' selected' : ''}>${escapeAttr(r.label)}</option>`
    ).join('');
}

async function enrichBatchUsers(defaults) {
    const body = { job_id: jobId };
    if (defaults && Object.keys(defaults).length) body.defaults = defaults;
    const res = await fetch(`${API_BASE}/api/users/enrich`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
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

function updateBatchSummary() {
    const bar = document.getElementById('batch-summary-bar');
    if (!bar) return;
    let valid = 0;
    enrichedUsers.forEach(u => {
        const errs = validateUserClient(u);
        u._field_errors = errs;
        u.missing_fields = Object.keys(errs);
        if (!Object.keys(errs).length) valid += 1;
    });
    const invalid = enrichedUsers.length - valid;
    bar.classList.remove('hidden');
    bar.textContent = `${valid}/${enrichedUsers.length} user đủ dữ liệu · ${invalid} user còn thiếu/sai trường`;
}

function updateBatchRowState(tr, user) {
    const errors = validateUserClient(user);
    user._field_errors = errors;
    user.missing_fields = Object.keys(errors);
    const hasErr = user.missing_fields.length > 0;
    tr.classList.toggle('row-error', hasErr);
    tr.classList.toggle('row-warn', !hasErr);

    tr.querySelectorAll('.batch-inp').forEach(el => {
        const f = el.dataset.f;
        const msg = errors[f];
        el.classList.toggle('input-err', !!msg);
        if (msg) el.title = msg;
        else el.removeAttribute('title');
    });

    const statusCol = tr.querySelector('.status-col');
    if (statusCol) {
        statusCol.innerHTML = Object.keys(errors).map(f =>
            `<span class="field-chip" title="${escapeAttr(errors[f])}">${escapeAttr(fieldLabel(f))}</span>`
        ).join('') || 'OK';
    }
}

function renderBatchReviewTable(users) {
    const tbody = document.getElementById('batch-review-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    updateBatchSummary();

    users.forEach((u, idx) => {
        if (batchShowErrorsOnly && !(u.missing_fields || []).length) return;

        const errors = u._field_errors || validateUserClient(u);
        const tr = document.createElement('tr');
        const hasErr = Object.keys(errors).length > 0;
        tr.className = hasErr ? 'row-error' : 'row-warn';

        const errChips = Object.keys(errors).map(f =>
            `<span class="field-chip" title="${escapeAttr(errors[f])}">${escapeAttr(fieldLabel(f))}</span>`
        ).join('') || 'OK';

        const inpCls = (f) => errors[f] ? 'batch-inp input-err' : 'batch-inp';
        const inpTitle = (f) => errors[f] ? ` title="${escapeAttr(errors[f])}"` : '';

        tr.innerHTML = `
            <td><input class="${inpCls('email')}" data-idx="${idx}" data-f="email" value="${escapeAttr(u.email || u.username || '')}"${inpTitle('email')}></td>
            <td><input class="${inpCls('last_name')}" data-idx="${idx}" data-f="last_name" value="${escapeAttr(u.last_name || '')}"${inpTitle('last_name')}></td>
            <td><input class="${inpCls('first_name')}" data-idx="${idx}" data-f="first_name" value="${escapeAttr(u.first_name || '')}"${inpTitle('first_name')}></td>
            <td><input class="${inpCls('cccd')}" data-idx="${idx}" data-f="cccd" value="${escapeAttr(u.cccd || '')}"${inpTitle('cccd')}></td>
            <td>
                <div class="picker-wrap">
                    <input class="${inpCls('branch_code')} branch-code-inp" data-idx="${idx}" data-f="branch_code" value="${escapeAttr(u.branch_code || '')}" placeholder="Mã CN"${inpTitle('branch_code')}>
                    <button type="button" class="btn btn-xs btn-ghost btn-pick-branch" data-idx="${idx}">Tìm</button>
                </div>
            </td>
            <td><input class="${inpCls('ipcas_code')}" data-idx="${idx}" data-f="ipcas_code" value="${escapeAttr(u.ipcas_code || '')}"${inpTitle('ipcas_code')}></td>
            <td><input class="${inpCls('phone')}" data-idx="${idx}" data-f="phone" value="${escapeAttr(u.phone || '')}"${inpTitle('phone')}></td>
            <td><input class="${inpCls('unit_code')}" data-idx="${idx}" data-f="unit_code" value="${escapeAttr(u.unit_code || '')}"${inpTitle('unit_code')}></td>
            <td>
                <select class="${inpCls('role')}" data-idx="${idx}" data-f="role"${inpTitle('role')}>
                    <option value="">-- Chọn --</option>
                    ${roleOptionsHtml(u.role || '')}
                </select>
            </td>
            <td>
                <div class="picker-wrap">
                    <input class="batch-inp agent-code-inp" data-idx="${idx}" data-f="agent_code" value="${escapeAttr(u.agent_code || '')}" placeholder="Mã ĐL">
                    <button type="button" class="btn btn-xs btn-ghost btn-pick-agent" data-idx="${idx}">Tìm</button>
                </div>
            </td>
            <td>${matchBadge(u.match_status)} ${u.match_confidence ? (u.match_confidence * 100).toFixed(0) + '%' : ''}</td>
            <td>
                <select class="batch-inp conflict-sel" data-idx="${idx}" data-f="on_conflict">
                    <option value="skip">Bỏ qua MK/OTP</option>
                    <option value="reset_password">Đổi MK</option>
                    <option value="reset_otp">Đổi OTP</option>
                    <option value="reset_both">Đổi cả 2</option>
                </select>
            </td>
            <td class="status-col">${errChips}</td>`;
        tbody.appendChild(tr);
        const conflictSel = tr.querySelector('.conflict-sel');
        if (conflictSel) conflictSel.value = u.on_conflict || document.getElementById('batch-default-conflict')?.value || 'skip';
    });

    tbody.querySelectorAll('.batch-inp').forEach(el => {
        const handler = () => {
            const i = +el.dataset.idx;
            const f = el.dataset.f;
            if (f === 'on_conflict') enrichedUsers[i].on_conflict = el.value;
            else {
                enrichedUsers[i][f] = el.value;
                if (f === 'email') enrichedUsers[i].username = el.value;
            }
            updateBatchSummary();
            const row = el.closest('tr');
            if (row) updateBatchRowState(row, enrichedUsers[i]);

            // Không tự ẩn dòng ngay khi vừa sửa xong để tránh cảm giác "bị mất".
            // Danh sách lọc chỉ cập nhật khi user bấm lại nút lọc/re-render thủ công.
        };
        el.addEventListener('change', handler);
        if (el.tagName === 'INPUT') el.addEventListener('input', handler);
    });

    tbody.querySelectorAll('.btn-pick-branch').forEach(btn => {
        btn.addEventListener('click', () => openAgencyPicker(+btn.dataset.idx));
    });
    tbody.querySelectorAll('.btn-pick-agent').forEach(btn => {
        btn.addEventListener('click', () => openAgentPicker(+btn.dataset.idx));
    });
}

function applyBulkBatchField(field, scope) {
    let value = '';
    if (field === 'branch_code') value = document.getElementById('bulk-branch-code')?.value?.trim() || '';
    else if (field === 'unit_code') value = document.getElementById('bulk-unit-code')?.value?.trim() || '';
    else if (field === 'role') value = document.getElementById('bulk-role')?.value || '';
    if (!value) {
        alert(`Nhập/chọn giá trị ${fieldLabel(field)} trước.`);
        return;
    }
    enrichedUsers.forEach(u => {
        const errs = validateUserClient(u);
        const isError = !!errs[field] || (fieldConfig?.required_fields || []).includes(field) && !String(u[field] || '').trim();
        if (scope === 'error' && !isError) return;
        u[field] = value;
        if (field === 'email') u.username = value;
    });
    renderBatchReviewTable(enrichedUsers);
}

async function openAgencyPicker(idx) {
    const q = prompt('Tìm chi nhánh (tên hoặc mã):', enrichedUsers[idx].branch_name || enrichedUsers[idx].department_name || '');
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
        if (items[n - 1].ipcas_code) enrichedUsers[idx].ipcas_code = items[n - 1].ipcas_code;
        enrichedUsers[idx].match_status = 'manual';
        renderBatchReviewTable(enrichedUsers);
    }
}

function showBatchReviewModal(users) {
    enrichedUsers = users.map(u => ({
        ...u,
        username: u.username || u.email,
        on_conflict: u.on_conflict || document.getElementById('batch-default-conflict')?.value || 'skip',
    }));
    batchShowErrorsOnly = false;
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

function validateBeforeProvision() {
    updateBatchSummary();
    const missingUsers = enrichedUsers.filter(u => (u.missing_fields || []).length > 0);
    if (missingUsers.length) {
        batchShowErrorsOnly = true;
        const btnFilter = document.getElementById('btn-batch-filter-errors');
        if (btnFilter) btnFilter.textContent = 'Hiện tất cả dòng';
        renderBatchReviewTable(enrichedUsers);
        setTimeout(() => {
            const firstErr = document.querySelector('#batch-review-tbody .row-error .batch-inp.input-err');
            firstErr?.focus();
        }, 0);
        const sample = missingUsers.slice(0, 3).map(u => {
            const labels = (u.missing_fields || []).map(fieldLabel).join(', ');
            return `${u.email || u.username}: ${labels}`;
        }).join('\n');
        alert(`Còn ${missingUsers.length} user thiếu/sai trường. Hệ thống đã tự lọc sang các dòng lỗi để bạn sửa.\n\n${sample}${missingUsers.length > 3 ? '\n...' : ''}`);
        return false;
    }
    return true;
}

async function confirmBatchProvision() {
    if (!validateBeforeProvision()) return;
    const defaultConflict = document.getElementById('batch-default-conflict')?.value || 'skip';
    const btn = document.getElementById('btn-batch-confirm');
    if (btn) { btn.disabled = true; btn.textContent = 'Đang tạo lô...'; }

    try {
        const payloadUsers = enrichedUsers.map(u => ({
            ...u,
            username: u.email || u.username,
        }));
        const res = await fetch(`${API_BASE}/api/users/provision-batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                users: payloadUsers,
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
    document.getElementById('batch-default-conflict')?.addEventListener('change', (e) => {
        const val = e.target.value;
        document.querySelectorAll('.conflict-sel').forEach(sel => { sel.value = val; });
        enrichedUsers.forEach(u => { u.on_conflict = val; });
    });
    document.getElementById('btn-batch-re-enrich')?.addEventListener('click', async () => {
        try {
            const defaults = {};
            const bc = document.getElementById('bulk-branch-code')?.value?.trim();
            const uc = document.getElementById('bulk-unit-code')?.value?.trim();
            const role = document.getElementById('bulk-role')?.value;
            if (bc) defaults.branch_code = bc;
            if (uc) defaults.unit_code = uc;
            if (role) defaults.role = role;
            const data = await enrichBatchUsers(defaults);
            enrichedUsers = data.users;
            renderBatchReviewTable(enrichedUsers);
        } catch (e) { alert(e.message); }
    });
    document.querySelectorAll('[data-bulk]').forEach(btn => {
        btn.addEventListener('click', () => {
            applyBulkBatchField(btn.dataset.bulk, btn.dataset.scope);
        });
    });
    document.getElementById('btn-batch-filter-errors')?.addEventListener('click', () => {
        batchShowErrorsOnly = !batchShowErrorsOnly;
        const btn = document.getElementById('btn-batch-filter-errors');
        if (btn) btn.textContent = batchShowErrorsOnly ? 'Hiện tất cả dòng' : 'Chỉ hiện dòng lỗi';
        renderBatchReviewTable(enrichedUsers);
    });
}
