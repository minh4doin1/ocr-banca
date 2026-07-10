/**
 * Batch user provisioning — enrich, review full-page, provision Keycloak.
 * Phụ thuộc app.js: getApiBase(), jobId, setStep, stopPolling, notify, escapeAttr, btnCreateBatch, successBatchCode, successTotalRecords
 */

const SSO_COL_FIELDS_10 = {
    0: 'stt', 1: 'name', 2: 'branch_code', 3: 'branch_name', 4: 'ipcas_code',
    5: 'cccd', 6: 'email', 7: 'phone', 8: 'role', 9: 'unit_code',
};

const SSO_COL_FIELDS_9 = {
    0: 'stt', 1: 'name', 2: 'department_name', 3: 'ipcas_code',
    4: 'cccd', 5: 'email', 6: 'phone', 7: 'role', 8: 'unit_code',
};

function getSsoColFields(numCols) {
    return numCols >= 10 ? SSO_COL_FIELDS_10 : SSO_COL_FIELDS_9;
}

let fieldConfig = null;
let enrichedUsers = [];
let batchShowErrorsOnly = false;
let reviewSearchQuery = '';
let lastProvisionResponse = null;
let followUpActions = {};
let followUpFinalized = false;
let successShowUpdatedOnly = false;
let successSearchQuery = '';
const SUCCESS_STATE_KEY_PREFIX = 'ocr_batch_success_state_v1';

function getSuccessStateKey() {
    const envId = typeof getActiveEnvId === 'function' ? getActiveEnvId() : 'dev';
    return `${SUCCESS_STATE_KEY_PREFIX}_${envId}`;
}

const CONFLICT_OPTIONS = [
    { value: 'skip', label: 'Bỏ qua' },
    { value: 'reset_password', label: 'Reset mật khẩu' },
    { value: 'reset_otp', label: 'Reset OTP' },
    { value: 'reset_both', label: 'Cả hai' },
];

async function loadFieldConfig() {
    try {
        const res = await fetch(`${getApiBase()}/api/users/field-config`, {
            headers: getTargetEnvHeaders(),
        });
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
    renderBulkRoleChips();
}

function fieldLabel(field) {
    return fieldConfig?.field_labels?.[field] || field;
}

function normalizeHeader(t) {
    return String(t || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

function getTableColFieldMap(table) {
    if (table?.table_kind === 'sso_agribank' || table?.num_cols >= 9) {
        const map = {};
        const colFields = getSsoColFields(table.num_cols);
        Object.entries(colFields).forEach(([col, field]) => {
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
    if (field && fieldConfig?.field_labels?.[field]) return fieldConfig.field_labels[field];
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
            if ((list || []).includes(norm)) map[cell.col] = field;
        }
    }
    return map;
}

function parseRolesText(text) {
    return String(text || '')
        .split(/[;,/|]+/)
        .map(s => s.trim())
        .filter(Boolean);
}

function validateCellByField(field, text, confidence, isHeader) {
    if (isHeader || field === 'stt') return '';
    if (!text?.trim()) {
        const label = fieldLabel(field);
        return label ? `Thiếu ${label}` : 'Không được trống';
    }
    if (field === 'cccd' && !/^\d{12}$/.test(text.replace(/\s+/g, ''))) return 'CCCD phải có 12 số';
    if (field === 'phone' && !/^0\d{8,10}$/.test(text.replace(/\s+/g, ''))) return 'SĐT không hợp lệ';
    if ((field === 'name' || field === 'first_name') && text && /\d/.test(text)) return 'Tên chứa chữ số';
    if (field === 'email' && text.includes('@') && !text.toLowerCase().endsWith('@agribank.com.vn')) {
        return 'Email phải thuộc @agribank.com.vn';
    }
    if (field === 'role') {
        const valid = (fieldConfig?.roles || []).map(r => r.value);
        const roles = parseRolesText(text);
        if (!roles.length) return 'Thiếu vai trò';
        const bad = roles.filter(r => !valid.includes(r));
        if (bad.length) return `Vai trò không hợp lệ: ${bad.join(', ')}`;
    }
    if (confidence < 0.85) return 'Độ tin cậy thấp';
    return '';
}

function validateUserClient(user) {
    const errors = {};
    const required = fieldConfig?.required_fields || [];
    required.forEach(field => {
        if (field === 'role') {
            const roles = user.roles?.length ? user.roles : parseRolesText(user.role);
            if (!roles.length) errors.role = 'Thiếu vai trò';
            return;
        }
        const val = String(user[field] || '').trim();
        if (!val) errors[field] = `Thiếu ${fieldLabel(field)}`;
    });
    if (user.cccd && !/^\d{12}$/.test(String(user.cccd).replace(/\s/g, ''))) errors.cccd = 'CCCD phải có 12 số';
    if (user.phone && !/^0\d{8,10}$/.test(String(user.phone).replace(/\s/g, ''))) errors.phone = 'SĐT không hợp lệ';
    const email = String(user.email || user.username || '');
    if (email && !email.toLowerCase().endsWith('@agribank.com.vn')) errors.email = 'Email phải thuộc @agribank.com.vn';
    const roles = user.roles?.length ? user.roles : parseRolesText(user.role);
    if (roles.length && fieldConfig?.roles?.length) {
        const valid = fieldConfig.roles.map(r => r.value);
        const bad = roles.filter(r => !valid.includes(r));
        if (bad.length) errors.role = `Vai trò không hợp lệ: ${bad.join(', ')}`;
    }
    return errors;
}

function roleOptionsHtml(selectedRoles) {
    const selected = new Set(selectedRoles || []);
    const roles = fieldConfig?.roles || [
        { value: 'banca-admin', label: 'Quản trị' },
        { value: 'banca-seller', label: 'Đại lý viên' },
        { value: 'banca-accounting-operator', label: 'Kế toán viên' },
        { value: 'banca-accounting-controller', label: 'Phê duyệt viên' },
    ];
    return roles.map(r =>
        `<option value="${escapeAttr(r.value)}"${selected.has(r.value) ? ' selected' : ''}>${escapeAttr(r.label || r.value)}</option>`
    ).join('');
}

function getRoleCatalog() {
    return fieldConfig?.roles || [
        { value: 'banca-admin', label: 'Quản trị' },
        { value: 'banca-seller', label: 'Đại lý viên' },
        { value: 'banca-accounting-operator', label: 'Kế toán viên' },
        { value: 'banca-accounting-controller', label: 'Phê duyệt viên' },
    ];
}

function renderRoleCell(u, idx, errors) {
    const roles = u.roles?.length ? u.roles : parseRolesText(u.role);
    const selected = new Set(roles);
    const chips = getRoleCatalog().map(r => {
        const on = selected.has(r.value);
        const errCls = errors.role && !on ? ' role-chip-warn' : '';
        return `<label class="role-chip${on ? ' role-chip-on' : ''}${errCls}">
            <input type="checkbox" class="role-chip-inp" data-idx="${idx}" value="${escapeAttr(r.value)}"${on ? ' checked' : ''}>
            <span>${escapeAttr(r.label || r.value)}</span>
        </label>`;
    }).join('');
    const rawHint = u.role_raw
        ? `<div class="role-ocr-hint" title="${escapeAttr(u.role_raw)}">OCR: ${escapeAttr(u.role_raw)}</div>`
        : '';
    const unmapped = roles.length === 0 && u.role_raw
        ? '<div class="role-unmapped">Chưa map được</div>'
        : '';
    return `<div class="role-chips" data-idx="${idx}">${chips}${rawHint}${unmapped}</div>`;
}

function readRoleChips(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll('.role-chip-inp:checked')).map(el => el.value);
}

function renderBulkRoleChips() {
    const wrap = document.getElementById('bulk-role-chips');
    if (!wrap) return;
    wrap.innerHTML = getRoleCatalog().map(r =>
        `<label class="role-chip">
            <input type="checkbox" class="bulk-role-chip" value="${escapeAttr(r.value)}">
            <span>${escapeAttr(r.label || r.value)}</span>
        </label>`
    ).join('') + `<button type="button" class="btn btn-xs btn-outline" id="btn-bulk-apply-roles">Áp dụng tất cả</button>`;
    document.getElementById('btn-bulk-apply-roles')?.addEventListener('click', () => {
        const roles = Array.from(document.querySelectorAll('.bulk-role-chip:checked')).map(el => el.value);
        if (!roles.length) {
            notify('warn', 'Chọn vai trò', 'Tick ít nhất một vai trò trước khi áp dụng.');
            return;
        }
        enrichedUsers.forEach(u => {
            u.roles = [...roles];
            u.role = roles[0] || '';
        });
        renderBatchReviewTable(enrichedUsers);
        notify('success', 'Đã áp dụng vai trò', `${roles.length} role cho ${enrichedUsers.length} user`);
    });
}

function roleDisplayText(user) {
    if (user.roles?.length) return user.roles.join('; ');
    return user.role || '';
}

function readRoleSelect(el) {
    return Array.from(el.selectedOptions).map(o => o.value).filter(Boolean);
}

async function enrichBatchUsers(defaults) {
    const body = { job_id: jobId };
    if (defaults && Object.keys(defaults).length) body.defaults = defaults;
    const res = await fetch(`${getApiBase()}/api/users/enrich`, {
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
    const res = await fetch(`${getApiBase()}/api/users/lookup/agencies?search=${encodeURIComponent(q)}&size=15`);
    if (!res.ok) return [];
    return (await res.json()).items || [];
}

async function searchAgents(q, agencyId) {
    let url = `${getApiBase()}/api/users/lookup/agents?search=${encodeURIComponent(q)}&size=15`;
    if (agencyId) url += `&agency_id=${encodeURIComponent(agencyId)}`;
    const res = await fetch(url);
    if (!res.ok) return [];
    return (await res.json()).items || [];
}

function rowMatchesSearch(u, q) {
    if (!q) return true;
    const hay = [
        u.email, u.username, u.ipcas_code, u.first_name, u.last_name,
        u.cccd, u.branch_code, u.phone, roleDisplayText(u), u.role_raw,
    ].join(' ').toLowerCase();
    return hay.includes(q);
}

function updateBatchSummary() {
    let valid = 0;
    enrichedUsers.forEach(u => {
        const errs = validateUserClient(u);
        u._field_errors = errs;
        u.missing_fields = Object.keys(errs);
        if (!Object.keys(errs).length) valid += 1;
    });
    const total = enrichedUsers.length;
    const errors = total - valid;
    const pct = total ? Math.round((valid / total) * 100) : 0;

    const statTotal = document.getElementById('stat-total');
    const statValid = document.getElementById('stat-valid');
    const statErrors = document.getElementById('stat-errors');
    const progressFill = document.getElementById('review-progress-fill');
    const progressText = document.getElementById('review-progress-text');
    if (statTotal) statTotal.textContent = String(total);
    if (statValid) statValid.textContent = String(valid);
    if (statErrors) statErrors.textContent = String(errors);
    if (progressFill) progressFill.style.width = `${pct}%`;
    if (progressText) progressText.textContent = `${pct}% hoàn tất`;

    const bar = document.getElementById('batch-summary-bar');
    if (bar) {
        bar.classList.add('hidden');
        bar.textContent = `${valid}/${total} user đủ dữ liệu`;
    }
}

function renderStatusCell(errors) {
    const keys = Object.keys(errors);
    if (!keys.length) {
        return '<span class="status-pill status-ok" title="Đủ dữ liệu">OK</span>';
    }
    const tips = keys.map(f => `${fieldLabel(f)}: ${errors[f]}`).join('\n');
    const labels = keys.map(f => fieldLabel(f)).slice(0, 3).join(', ');
    const more = keys.length > 3 ? ` +${keys.length - 3}` : '';
    return `<span class="status-pill status-err" title="${escapeAttr(tips)}">${escapeAttr(labels)}${more}</span>`;
}

function updateBatchRowState(tr, user) {
    const errors = validateUserClient(user);
    user._field_errors = errors;
    user.missing_fields = Object.keys(errors);
    tr.classList.toggle('row-error', user.missing_fields.length > 0);
    tr.classList.toggle('row-ok', !user.missing_fields.length);
    tr.querySelectorAll('.batch-inp').forEach(el => {
        const f = el.dataset.f;
        const msg = errors[f];
        el.classList.toggle('input-err', !!msg);
        if (msg) el.title = msg;
        else el.removeAttribute('title');
    });
    tr.querySelectorAll('.role-chip').forEach(chip => {
        const inp = chip.querySelector('.role-chip-inp');
        const val = inp?.value;
        chip.classList.toggle('role-chip-on', !!inp?.checked);
        chip.classList.toggle('role-chip-warn', !!errors.role && !inp?.checked);
    });
    const statusCol = tr.querySelector('.status-col');
    if (statusCol) statusCol.innerHTML = renderStatusCell(errors);
}

function renderBatchReviewTable(users) {
    const tbody = document.getElementById('batch-review-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    updateBatchSummary();

    const q = reviewSearchQuery.trim().toLowerCase();
    let displayIdx = 0;

    users.forEach((u, idx) => {
        if (batchShowErrorsOnly && !(u.missing_fields || []).length) return;
        if (!rowMatchesSearch(u, q)) return;

        displayIdx += 1;
        const errors = u._field_errors || validateUserClient(u);
        const tr = document.createElement('tr');
        tr.className = Object.keys(errors).length ? 'row-error' : 'row-ok';
        tr.dataset.idx = String(idx);

        const inpCls = (f) => errors[f] ? 'batch-inp input-err' : 'batch-inp';
        const inpTitle = (f) => errors[f] ? ` title="${escapeAttr(errors[f])}"` : '';

        tr.innerHTML = `
            <td class="col-stt">${displayIdx}</td>
            <td class="col-email"><input class="${inpCls('email')}" data-idx="${idx}" data-f="email" value="${escapeAttr(u.email || u.username || '')}"${inpTitle('email')}></td>
            <td class="col-name"><input class="${inpCls('last_name')}" data-idx="${idx}" data-f="last_name" value="${escapeAttr(u.last_name || '')}"${inpTitle('last_name')}></td>
            <td class="col-name"><input class="${inpCls('first_name')}" data-idx="${idx}" data-f="first_name" value="${escapeAttr(u.first_name || '')}"${inpTitle('first_name')}></td>
            <td class="col-cccd"><input class="${inpCls('cccd')}" data-idx="${idx}" data-f="cccd" value="${escapeAttr(u.cccd || '')}"${inpTitle('cccd')}></td>
            <td class="col-code">
                <div class="picker-inline">
                    <input class="${inpCls('branch_code')} branch-code-inp" data-idx="${idx}" data-f="branch_code" value="${escapeAttr(u.branch_code || '')}" placeholder="CN"${inpTitle('branch_code')}>
                    <button type="button" class="btn-icon btn-pick-branch" data-idx="${idx}" title="Tìm chi nhánh">⌕</button>
                </div>
            </td>
            <td class="col-code"><input class="${inpCls('ipcas_code')}" data-idx="${idx}" data-f="ipcas_code" value="${escapeAttr(u.ipcas_code || '')}"${inpTitle('ipcas_code')}></td>
            <td class="col-phone"><input class="${inpCls('phone')}" data-idx="${idx}" data-f="phone" value="${escapeAttr(u.phone || '')}"${inpTitle('phone')}></td>
            <td class="col-code"><input class="${inpCls('unit_code')}" data-idx="${idx}" data-f="unit_code" value="${escapeAttr(u.unit_code || '')}"${inpTitle('unit_code')}></td>
            <td class="col-role">${renderRoleCell(u, idx, errors)}</td>
            <td class="col-code">
                <div class="picker-inline">
                    <input class="batch-inp agent-code-inp" data-idx="${idx}" data-f="agent_code" value="${escapeAttr(u.agent_code || '')}" placeholder="ĐL">
                    <button type="button" class="btn-icon btn-pick-agent" data-idx="${idx}" title="Tìm đại lý">⌕</button>
                </div>
            </td>
            <td class="status-col col-status">${renderStatusCell(errors)}</td>`;
        tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.batch-inp').forEach(el => {
        const handler = () => {
            const i = +el.dataset.idx;
            const f = el.dataset.f;
            enrichedUsers[i][f] = el.value;
            if (f === 'email') enrichedUsers[i].username = el.value;
            updateBatchSummary();
            const row = el.closest('tr');
            if (row) updateBatchRowState(row, enrichedUsers[i]);
        };
        el.addEventListener('change', handler);
        if (el.tagName === 'INPUT') el.addEventListener('input', handler);
    });

    tbody.querySelectorAll('.role-chip-inp').forEach(el => {
        el.addEventListener('change', () => {
            const i = +el.dataset.idx;
            const wrap = el.closest('.role-chips');
            enrichedUsers[i].roles = readRoleChips(wrap);
            enrichedUsers[i].role = enrichedUsers[i].roles[0] || '';
            updateBatchSummary();
            const row = el.closest('tr');
            if (row) updateBatchRowState(row, enrichedUsers[i]);
        });
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
    if (!value) {
        notify('warn', 'Thiếu giá trị', `Nhập ${fieldLabel(field)} trước khi áp dụng hàng loạt.`);
        return;
    }
    enrichedUsers.forEach(u => {
        const errs = validateUserClient(u);
        const isError = !!errs[field] || ((fieldConfig?.required_fields || []).includes(field) && !String(u[field] || '').trim());
        if (scope === 'error' && !isError) return;
        if (field === 'role') {
            const roles = parseRolesText(value);
            u.role = roles[0] || '';
            u.roles = roles;
        } else {
            u[field] = value;
            if (field === 'email') u.username = value;
        }
    });
    renderBatchReviewTable(enrichedUsers);
    notify('success', 'Đã áp dụng hàng loạt', `${fieldLabel(field)} → ${value}`);
}

async function openAgencyPicker(idx) {
    const q = prompt('Tìm chi nhánh (tên hoặc mã):', enrichedUsers[idx].branch_name || enrichedUsers[idx].department_name || '');
    if (q === null) return;
    const items = await searchAgencies(q);
    if (!items.length) { notify('warn', 'Không tìm thấy chi nhánh'); return; }
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
    if (!items.length) { notify('warn', 'Không tìm thấy đại lý'); return; }
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

function renderReviewPage(users) {
    if (!users?.length) {
        notify('warn', 'Không có dữ liệu', 'Không có user để kiểm tra.');
        return;
    }
    enrichedUsers = users.map(u => ({
        ...u,
        username: u.username || u.email,
        role_raw: u.role_raw || u.role || '',
        roles: u.roles?.length ? u.roles : parseRolesText(u.role),
        role: u.role || (u.roles?.[0] ?? ''),
    }));
    batchShowErrorsOnly = false;
    reviewSearchQuery = '';
    const searchInp = document.getElementById('review-search');
    if (searchInp) searchInp.value = '';
    const filterCb = document.getElementById('btn-batch-filter-errors');
    if (filterCb) filterCb.checked = false;
    renderBulkRoleChips();
    renderBatchReviewTable(enrichedUsers);
    setStep(2);
}

function hideReviewPage() {
    enrichedUsers = [];
    batchShowErrorsOnly = false;
    reviewSearchQuery = '';
    const tbody = document.getElementById('batch-review-tbody');
    if (tbody) tbody.innerHTML = '';
    ['stat-total', 'stat-valid', 'stat-errors'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '0';
    });
    const progressFill = document.getElementById('review-progress-fill');
    const progressText = document.getElementById('review-progress-text');
    if (progressFill) progressFill.style.width = '0%';
    if (progressText) progressText.textContent = '0%';
    document.getElementById('review-bulk-panel')?.classList.add('hidden');
}

function validateBeforeProvision() {
    updateBatchSummary();
    const missingUsers = enrichedUsers.filter(u => (u.missing_fields || []).length > 0);
    if (missingUsers.length) {
        batchShowErrorsOnly = true;
        const filterCb = document.getElementById('btn-batch-filter-errors');
        if (filterCb) filterCb.checked = true;
        renderBatchReviewTable(enrichedUsers);
        setTimeout(() => document.querySelector('#batch-review-tbody .row-error .batch-inp.input-err')?.focus(), 0);
        const sample = missingUsers.slice(0, 5).map(u => {
            const labels = (u.missing_fields || []).map(fieldLabel).join(', ');
            return `${u.email || u.username}: ${labels}`;
        }).join('\n');
        notify('error', `Còn ${missingUsers.length} user thiếu/sai trường`, sample + (missingUsers.length > 5 ? '\n...' : ''), 10000);
        return false;
    }
    return true;
}

function formatActionsVi(actions) {
    return (actions || []).map(a => {
        if (a === 'create') return 'Đã tạo user';
        if (a === 'save_details') return 'Đã cập nhật thông tin';
        if (a === 'set_attributes') return 'Đã gán thuộc tính';
        if (a === 'reset_password') return 'Đã reset mật khẩu';
        if (a.startsWith('reset_otp')) return a.replace('reset_otp', 'Đã reset OTP').replace('deleted=', 'đã xóa ');
        if (a.startsWith('assign_role:')) return `Đã gán role ${a.split(':')[1]}`;
        if (a.startsWith('remove_role:')) return `Đã gỡ role ${a.split(':')[1]}`;
        if (a.startsWith('role_already:')) return `Role ${a.split(':')[1]} đã có`;
        if (a.startsWith('assign_role_skipped:')) return `Bỏ qua gán role ${a.split(':')[1]} (403)`;
        if (a === 'roles_assignment_failed:403') return 'Không gán được role (403) — kiểm tra quyền Keycloak';
        if (a === 'require_action:UPDATE_PASSWORD' || a === 'UPDATE_PASSWORD') return 'Yêu cầu đổi MK lần đăng nhập';
        if (a === 'require_action:CONFIGURE_TOTP' || a === 'CONFIGURE_TOTP') return 'Yêu cầu cấu hình OTP mới';
        return a;
    }).join(' · ');
}


function syncRolesFromDom() {
    document.querySelectorAll('.role-chips').forEach(wrap => {
        const idx = +wrap.dataset.idx;
        if (Number.isNaN(idx) || !enrichedUsers[idx]) return;
        enrichedUsers[idx].roles = readRoleChips(wrap);
        enrichedUsers[idx].role = enrichedUsers[idx].roles[0] || '';
    });
}

function statusLabelVi(status) {
    const map = { created: 'Tạo mới', updated: 'Đã có', failed: 'Lỗi', skipped: 'Bỏ qua' };
    return map[status] || status;
}

function renderConflictSelect(username, action, disabled = false) {
    const opts = CONFLICT_OPTIONS.map(o =>
        `<option value="${o.value}"${o.value === action ? ' selected' : ''}>${o.label}</option>`
    ).join('');
    return `<select class="success-conflict-sel" data-username="${escapeAttr(username)}"${disabled ? ' disabled' : ''}>${opts}</select>`;
}

function initFollowUpActions(results) {
    followUpActions = {};
    (results || []).forEach(r => {
        if (r.status === 'updated') followUpActions[r.username] = 'skip';
    });
}

function countPendingFollowUps() {
    return Object.values(followUpActions).filter(a => a && a !== 'skip').length;
}

function updateSuccessStats(data) {
    const set = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = String(val ?? 0);
    };
    set('success-stat-total', data.total);
    set('success-stat-created', data.created);
    set('success-stat-updated', data.updated);
    set('success-stat-failed', data.failed);
    const hint = document.getElementById('success-stats-hint');
    if (hint) {
        const parts = [];
        if (data.skipped) parts.push(`${data.skipped} bỏ qua`);
        if (data.updated) parts.push(`${data.updated} user đã có — chọn thao tác bổ sung bên dưới nếu cần`);
        hint.textContent = parts.join(' · ');
    }
    const toolbar = document.getElementById('success-toolbar');
    if (toolbar) toolbar.classList.toggle('hidden', !(data.updated > 0));
}

function updateFinalizeButton() {
    const btn = document.getElementById('btn-finalize-batch');
    if (!btn) return;
    const pending = countPendingFollowUps();
    const updatedCount = lastProvisionResponse?.updated || 0;
    if (!updatedCount) {
        btn.textContent = 'Hoàn tất';
        btn.disabled = false;
        return;
    }
    if (followUpFinalized && pending === 0) {
        btn.textContent = 'Đã hoàn tất';
        btn.disabled = true;
    } else if (pending > 0) {
        btn.textContent = `Hoàn tất (${pending})`;
        btn.disabled = false;
    } else {
        btn.textContent = 'Hoàn tất';
        btn.disabled = false;
    }
}

function rowMatchesSuccessSearch(r, q) {
    if (!q) return true;
    const hay = `${r.username || ''} ${r.user_id || ''}`.toLowerCase();
    return hay.includes(q);
}

function renderSuccessResultsTable() {
    const tbody = document.getElementById('success-results-tbody');
    if (!tbody || !lastProvisionResponse) return;
    tbody.innerHTML = '';
    const q = successSearchQuery.trim().toLowerCase();
    let displayIdx = 0;

    (lastProvisionResponse.results || []).forEach(r => {
        if (successShowUpdatedOnly && r.status !== 'updated') return;
        if (!rowMatchesSuccessSearch(r, q)) return;
        displayIdx += 1;

        const statusCls = r.status === 'failed' ? 'status-failed'
            : r.status === 'created' ? 'status-created'
                : r.status === 'updated' ? 'status-updated' : 'status-skipped';
        const actions = formatActionsVi(r.actions_applied);
        const isUpdated = r.status === 'updated';
        const action = followUpActions[r.username] || 'skip';
        const followUpCell = isUpdated
            ? renderConflictSelect(r.username, action)
            : '<span class="text-muted">—</span>';

        const tr = document.createElement('tr');
        tr.className = `success-row ${statusCls}`;
        tr.innerHTML = `
            <td class="col-stt">${displayIdx}</td>
            <td class="col-email"><span class="success-username">${escapeAttr(r.username)}</span></td>
            <td class="col-status"><span class="success-badge ${statusCls}">${escapeAttr(statusLabelVi(r.status))}</span></td>
            <td class="col-actions-done">${actions ? escapeAttr(actions) : '<span class="text-muted">—</span>'}</td>
            <td class="col-error">${r.error ? `<span class="success-error">${escapeAttr(r.error)}</span>` : '<span class="text-muted">—</span>'}</td>
            <td class="col-followup">${followUpCell}</td>`;
        tbody.appendChild(tr);
    });

    tbody.querySelectorAll('.success-conflict-sel').forEach(sel => {
        sel.addEventListener('change', () => {
            followUpActions[sel.dataset.username] = sel.value;
            followUpFinalized = false;
            updateFinalizeButton();
            persistSuccessState();
        });
    });
    updateFinalizeButton();
}

function hideSuccessPage() {
    lastProvisionResponse = null;
    followUpActions = {};
    followUpFinalized = false;
    successShowUpdatedOnly = false;
    successSearchQuery = '';
    localStorage.removeItem(getSuccessStateKey());
    const tbody = document.getElementById('success-results-tbody');
    if (tbody) tbody.innerHTML = '';
    const searchInp = document.getElementById('success-search');
    if (searchInp) searchInp.value = '';
    const filterCb = document.getElementById('success-filter-updated');
    if (filterCb) filterCb.checked = false;
    ['success-stat-total', 'success-stat-created', 'success-stat-updated', 'success-stat-failed'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '0';
    });
    const hint = document.getElementById('success-stats-hint');
    if (hint) hint.textContent = '';
}

function persistSuccessState() {
    if (!lastProvisionResponse || !jobId) return;
    const payload = {
        envId: typeof getActiveEnvId === 'function' ? getActiveEnvId() : 'dev',
        jobId,
        lastProvisionResponse,
        followUpActions,
        followUpFinalized,
        successShowUpdatedOnly,
        successSearchQuery,
        savedAt: new Date().toISOString(),
    };
    localStorage.setItem(getSuccessStateKey(), JSON.stringify(payload));
}

function restoreSuccessState() {
    try {
        const raw = localStorage.getItem(getSuccessStateKey());
        if (!raw) return false;
        const s = JSON.parse(raw);
        if (!s?.jobId || !s?.lastProvisionResponse?.results?.length) return false;
        const currentEnv = typeof getActiveEnvId === 'function' ? getActiveEnvId() : 'dev';
        if (s.envId && s.envId !== currentEnv) return false;

        jobId = s.jobId;
        lastProvisionResponse = s.lastProvisionResponse;
        followUpActions = s.followUpActions || {};
        followUpFinalized = !!s.followUpFinalized;
        successShowUpdatedOnly = !!s.successShowUpdatedOnly;
        successSearchQuery = s.successSearchQuery || '';

        if (successBatchCode) successBatchCode.textContent = `BATCH-${jobId.toUpperCase()}`;
        updateSuccessStats(lastProvisionResponse);
        const searchInp = document.getElementById('success-search');
        if (searchInp) searchInp.value = successSearchQuery;
        const filterCb = document.getElementById('success-filter-updated');
        if (filterCb) filterCb.checked = successShowUpdatedOnly;
        renderSuccessResultsTable();
        setStep(3);
        return true;
    } catch {
        localStorage.removeItem(getSuccessStateKey());
        return false;
    }
}

function buildProvisionUserPayload(u, onConflict) {
    const payload = {
        username: u.email || u.username,
        email: u.email || u.username,
        first_name: u.first_name || '',
        last_name: u.last_name || '',
        branch_code: u.branch_code || '',
        ipcas_code: u.ipcas_code || '',
        cccd: u.cccd || '',
        phone: u.phone || '',
        unit_code: u.unit_code || '',
        role: u.role || (u.roles?.[0] ?? ''),
        roles: u.roles?.length ? u.roles : parseRolesText(u.role),
        agent_code: u.agent_code || '',
        department_name: u.department_name || '',
    };
    if (onConflict) payload.on_conflict = onConflict;
    return payload;
}

async function fetchKeycloakDiagnostics() {
    try {
        const res = await fetch(`${getApiBase()}/api/users/keycloak-diagnostics`, {
            headers: getTargetEnvHeaders(),
        });
        const data = await res.json();
        console.group(`Keycloak diagnostics (${data.target_env || '?'})`);
        console.log(data.summary);
        (data.steps || []).forEach((s) => {
            const tag = s.ok ? 'OK' : 'FAIL';
            console.log(`${tag} [${s.step}] ${s.message}`, s.detail || '');
        });
        if (data.log_hint) console.info(data.log_hint);
        console.groupEnd();
        return data;
    } catch (e) {
        console.warn('keycloak-diagnostics failed', e);
        return null;
    }
}

function formatDiagnosticsHint(diag) {
    if (!diag) return '';
    const failed = (diag.steps || []).filter((s) => !s.ok);
    if (!failed.length) return diag.summary || '';
    const lines = failed.slice(0, 4).map((s) => `${s.step}: ${s.message}${s.detail ? ` — ${s.detail.slice(0, 120)}` : ''}`);
    return `${diag.summary}\n${lines.join('\n')}`;
}

async function createBatch() {
    syncRolesFromDom();
    if (!validateBeforeProvision()) return;
    const btn = btnCreateBatch;
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Đang tạo lô...'; }

    try {
        const payloadUsers = enrichedUsers.map(u => buildProvisionUserPayload(u));
    const res = await fetch(`${getApiBase()}/api/users/provision-batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getTargetEnvHeaders() },
            body: JSON.stringify({ users: payloadUsers, default_on_conflict: 'skip' }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const diag = await fetchKeycloakDiagnostics();
            const hint = formatDiagnosticsHint(diag);
            throw new Error(hint ? `${data.detail || 'Tạo lô thất bại'}\n${hint}` : (data.detail || 'Tạo lô thất bại'));
        }
        showBatchResults(data);
    } catch (e) {
        notify('error', 'Lỗi tạo lô', e.message, 15000);
        await fetchKeycloakDiagnostics();
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = orig || 'Xác nhận & Tạo lô User'; }
    }
}

function showBatchResults(data) {
    const roleSkipped = (data.results || []).some(r =>
        (r.actions_applied || []).some(a => a.startsWith('assign_role_skipped:') || a === 'roles_assignment_failed:403')
    );
    if (roleSkipped) {
        notify('warn', 'Role Keycloak chưa được gán',
            'Service account thiếu quyền gán client role (cần manage-users + manage-clients trên realm-management). Gọi GET /api/users/keycloak-role-check hoặc cấu hình KEYCLOAK_ROLE_ASSIGN_CLIENT_ID với client có realm-admin.',
            12000);
    }

    lastProvisionResponse = data;
    followUpFinalized = false;
    initFollowUpActions(data.results);

    if (successBatchCode) successBatchCode.textContent = `BATCH-${jobId.toUpperCase()}`;
    updateSuccessStats(data);
    renderSuccessResultsTable();

    if (data.failed > 0) {
        fetchKeycloakDiagnostics().then((diag) => {
            const hint = formatDiagnosticsHint(diag);
            notify('warn', `Tạo lô xong — ${data.failed} lỗi`,
                `${data.created} tạo mới, ${data.updated} đã có.${hint ? '\n' + hint : ''} Mở F12 Console hoặc logs/keycloak.log để xem chi tiết.`,
                15000);
        });
    } else {
        notify('success', 'Tạo lô thành công', `${data.created} tạo mới, ${data.updated} đã có. Chọn reset MK/OTP nếu cần rồi bấm Hoàn tất.`, 8000);
    }
    stopPolling();
    setStep(3);
    persistSuccessState();
}

async function finalizeBatch() {
    const pending = Object.entries(followUpActions).filter(([, a]) => a && a !== 'skip');
    if (!pending.length) {
        followUpFinalized = true;
        updateFinalizeButton();
        notify('info', 'Không có thao tác bổ sung', 'Tất cả user đã có được chọn bỏ qua.');
        return;
    }

    const btn = document.getElementById('btn-finalize-batch');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Đang xử lý...'; }

    try {
        const payloadUsers = pending.map(([username, on_conflict]) => {
            const src = enrichedUsers.find(u => (u.email || u.username) === username);
            if (!src) return null;
            return buildProvisionUserPayload(src, on_conflict);
        }).filter(Boolean);

        if (!payloadUsers.length) throw new Error('Không tìm thấy dữ liệu user để áp dụng thao tác.');

    const res = await fetch(`${getApiBase()}/api/users/provision-batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getTargetEnvHeaders() },
            body: JSON.stringify({ users: payloadUsers, default_on_conflict: 'skip' }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Áp dụng thao tác thất bại');

        const byUsername = Object.fromEntries((data.results || []).map(r => [r.username, r]));
        lastProvisionResponse.results = (lastProvisionResponse.results || []).map(r => {
            const follow = byUsername[r.username];
            if (!follow) return r;
            const mergedActions = [...(r.actions_applied || [])];
            (follow.actions_applied || []).forEach(a => {
                if (!mergedActions.includes(a)) mergedActions.push(a);
            });
            return {
                ...r,
                actions_applied: mergedActions,
                error: follow.error || r.error,
                status: follow.status === 'failed' ? 'failed' : r.status,
            };
        });

        (data.results || []).forEach(r => {
            if (!r.error) {
                followUpActions[r.username] = 'skip';
            }
        });
        followUpFinalized = countPendingFollowUps() === 0;
        renderSuccessResultsTable();
        persistSuccessState();

        const ok = (data.results || []).filter(r => !r.error).length;
        const fail = (data.results || []).filter(r => r.error).length;
        if (fail > 0) {
            notify('warn', `Hoàn tất — ${fail} lỗi`, `${ok} user đã áp dụng thao tác bổ sung.`, 8000);
        } else {
            notify('success', 'Đã hoàn tất', `${ok} user đã áp dụng reset mật khẩu/OTP.`, 6000);
        }
    } catch (e) {
        notify('error', 'Lỗi hoàn tất', e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            if (!followUpFinalized || countPendingFollowUps() > 0) btn.textContent = orig || 'Hoàn tất';
        }
        updateFinalizeButton();
    }
}

async function submitReview() {
    if (!jobId) {
        notify('error', 'Chưa có job', 'Upload PDF hoặc Excel trước.');
        return;
    }
    const btn = btnCreateBatch;
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Đang tải dữ liệu...'; }

    try {
        const data = await enrichBatchUsers();
        if (!data.users?.length) throw new Error('Không có user để tạo lô.');
        if (data.warnings?.length) {
            notify('warn', 'Cảnh báo enrich', data.warnings.slice(0, 3).join('; '), 8000);
        }
        renderReviewPage(data.users);
        notify('info', 'Sẵn sàng kiểm tra', `${data.users.length} user — chỉnh sửa rồi xác nhận tạo lô.`);
    } catch (e) {
        notify('error', 'Lỗi tải dữ liệu', e.message);
        throw e;
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = orig || 'Xác nhận & Tạo lô User'; }
    }
}

async function submitBatch() {
    await createBatch();
}

function setupReviewPage() {
    document.getElementById('btn-toggle-bulk')?.addEventListener('click', () => {
        const panel = document.getElementById('review-bulk-panel');
        panel?.classList.toggle('hidden');
    });

    document.getElementById('review-search')?.addEventListener('input', (e) => {
        reviewSearchQuery = e.target.value || '';
        renderBatchReviewTable(enrichedUsers);
    });

    document.getElementById('btn-batch-filter-errors')?.addEventListener('change', (e) => {
        batchShowErrorsOnly = e.target.checked;
        renderBatchReviewTable(enrichedUsers);
    });

    document.getElementById('btn-batch-re-enrich')?.addEventListener('click', async () => {
        try {
            const defaults = {};
            const bc = document.getElementById('bulk-branch-code')?.value?.trim();
            const uc = document.getElementById('bulk-unit-code')?.value?.trim();
            if (bc) defaults.branch_code = bc;
            if (uc) defaults.unit_code = uc;
            const data = await enrichBatchUsers(defaults);
            enrichedUsers = data.users.map(u => ({
                ...u,
                roles: u.roles?.length ? u.roles : parseRolesText(u.role),
            }));
            renderBatchReviewTable(enrichedUsers);
            notify('success', 'Enrich lại thành công', `${enrichedUsers.length} user`);
        } catch (e) { notify('error', 'Enrich thất bại', e.message); }
    });
    document.querySelectorAll('[data-bulk]').forEach(btn => {
        btn.addEventListener('click', () => applyBulkBatchField(btn.dataset.bulk, btn.dataset.scope));
    });
}

function setupSuccessPage() {
    document.getElementById('btn-finalize-batch')?.addEventListener('click', () => finalizeBatch());

    document.getElementById('btn-success-bulk-apply')?.addEventListener('click', () => {
        const val = document.getElementById('success-bulk-conflict')?.value || 'skip';
        Object.keys(followUpActions).forEach(username => { followUpActions[username] = val; });
        followUpFinalized = false;
        renderSuccessResultsTable();
        persistSuccessState();
        notify('success', 'Đã áp dụng hàng loạt', CONFLICT_OPTIONS.find(o => o.value === val)?.label || val);
    });

    document.getElementById('success-search')?.addEventListener('input', (e) => {
        successSearchQuery = e.target.value || '';
        renderSuccessResultsTable();
        persistSuccessState();
    });

    document.getElementById('success-filter-updated')?.addEventListener('change', (e) => {
        successShowUpdatedOnly = e.target.checked;
        renderSuccessResultsTable();
        persistSuccessState();
    });

    const restored = restoreSuccessState();
    if (restored) {
        notify('info', 'Đã khôi phục trạng thái', 'Trang kết quả lô trước đã được giữ lại sau khi tải lại.');
    }
}

// Backward compat
function hideBatchReviewModal() { hideReviewPage(); }
function showBatchReviewModal(users) { renderReviewPage(users); }
function setupBatchReviewModal() { setupReviewPage(); }
function confirmBatchProvision() { return createBatch(); }
