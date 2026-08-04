/* ============================================================
   Agribank Banca OCR â Client Application (Excel-flow)
   ============================================================ */

const _devFePorts = new Set(['5173', '3000', '5500']);
const ENV_STORAGE_KEY = 'ocr_keycloak_env_v1';
const ENV_META_CACHE_KEY = 'ocr_keycloak_env_meta_v2';
/** Chá» Vite FE má»i trá» localhost:8100; khi má» tá»« :8100/LAN/Tailscale â same-origin */
const _viteApiBase = _devFePorts.has(window.location.port) ? 'http://localhost:8100' : '';

let prodKeycloakReady = false;
let prodKeycloakLabel = '';
let activeEnvId = localStorage.getItem(ENV_STORAGE_KEY) || 'dev';

function getActiveEnvId() {
    return activeEnvId;
}

/** Backend OCR â luÃ´n cÃ¹ng mÃ¡y Äang má» FE (khÃ´ng Äá»i khi chuyá»n KC DEV/PROD) */
function getApiBase() {
    return (_viteApiBase || '').replace(/\/$/, '');
}

/** Chá» Ã¡p dá»¥ng cho API táº¡o lÃ´ user / Keycloak */
function getTargetEnvHeaders() {
    return { 'X-OCR-Target-Env': activeEnvId };
}

window.getTargetEnvHeaders = getTargetEnvHeaders;

function _readEnvMeta() {
    try {
        const raw = localStorage.getItem(ENV_META_CACHE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
}

function _writeEnvMeta(meta) {
    try {
        localStorage.setItem(ENV_META_CACHE_KEY, JSON.stringify(meta));
    } catch {
        /* ignore */
    }
}

async function loadEnvironmentProfiles() {
    // XÃ³a cache cÅ© tá»«ng Ã©p api_base=localhost (gÃ¢y lá»i khi má» qua LAN/Tailscale)
    try {
        localStorage.removeItem('ocr_keycloak_env_meta_v1');
        localStorage.removeItem('ocr_api_env_profiles_v1');
        localStorage.removeItem('ocr_api_env_profiles_v2');
        localStorage.removeItem('ocr_api_env_v1');
    } catch {
        /* ignore */
    }

    const cached = _readEnvMeta();
    if (cached?.prod_keycloak_ready) {
        prodKeycloakReady = true;
        prodKeycloakLabel = cached.prod_label || '';
    }

    try {
        const res = await fetch(`${getApiBase()}/api/ocr/environments`);
        if (!res.ok) return;
        const data = await res.json();
        const prodProfile = (data.profiles || []).find((p) => p.id === 'prod');
        prodKeycloakReady = !!prodProfile?.keycloak_configured;
        prodKeycloakLabel = prodProfile?.keycloak_label || prodProfile?.label || 'PROD';
        _writeEnvMeta({
            prod_keycloak_ready: prodKeycloakReady,
            prod_label: prodKeycloakLabel,
        });
    } catch {
        /* giá»¯ cache / máº·c Äá»nh */
    }
}

function updateEnvUi() {
    const badge = document.getElementById('env-badge');
    const btn = document.getElementById('btn-env-switch');
    const isProd = activeEnvId === 'prod';

    if (badge) {
        badge.textContent = isProd ? 'KC PROD' : 'KC DEV';
        badge.classList.remove('env-dev', 'env-prod', 'env-mismatch');
        badge.classList.add(isProd ? 'env-prod' : 'env-dev');
        badge.title = isProd
            ? `Táº¡o lÃ´ user â Keycloak PROD (${prodKeycloakLabel || 'production'})`
            : 'Táº¡o lÃ´ user â Keycloak DEV';
    }
    if (btn) {
        if (!isProd && !prodKeycloakReady) {
            btn.disabled = true;
            btn.textContent = 'PROD chÆ°a cáº¥u hÃ¬nh';
            btn.title = 'ThÃªm KEYCLOAK_PROD_* trong .env rá»i restart server';
        } else {
            btn.disabled = false;
            btn.textContent = isProd ? 'Chuyá»n KC DEV' : 'Chuyá»n KC PROD';
            btn.title = isProd
                ? 'Táº¡o lÃ´ sáº½ gá»i Keycloak DEV'
                : 'Táº¡o lÃ´ sáº½ gá»i Keycloak PROD (OCR váº«n cháº¡y trÃªn server hiá»n táº¡i)';
        }
    }
}

async function switchEnvironment() {
    const targetId = activeEnvId === 'dev' ? 'prod' : 'dev';
    if (targetId === 'prod' && !prodKeycloakReady) {
        notify('warning', 'ChÆ°a cáº¥u hÃ¬nh Keycloak PROD', 'ThÃªm KEYCLOAK_PROD_BASE_URL + KEYCLOAK_PROD_CLIENT_SECRET trong .env.');
        return;
    }
    activeEnvId = targetId;
    localStorage.setItem(ENV_STORAGE_KEY, activeEnvId);
    updateEnvUi();
    // KhÃ´ng gá»i láº¡i field-config / khÃ´ng Äá»i API base â trÃ¡nh lá»i káº¿t ná»i BE
    const label = targetId === 'prod' ? 'Keycloak PROD' : 'Keycloak DEV';
    notify('success', `ÄÃ£ chuyá»n sang ${label}`, 'OCR khÃ´ng Äá»i. Chá» bÆ°á»c Táº¡o lÃ´ user dÃ¹ng Keycloak má»i.', 5000);
}

window.getApiBase = getApiBase;
window.getActiveEnvId = getActiveEnvId;
window.switchEnvironment = switchEnvironment;

// ââ State ââ
let currentStep = 0;
let selectedFile = null;
let jobId = '';
let totalPages = 0;
let ocrData = null;
let jobStatus = null;
let pollTimer = null;
let lastLogCount = 0;
let runtimeConfig = null;
let uploadSource = 'pdf'; // 'pdf' | 'excel'
const selectedExportPages = new Set();

// ââ DOM refs ââ
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const fileInput = $('#file-input');
const excelInput = $('#excel-input');
const excelInputReupload = $('#excel-input-reupload');
const docxInputReupload = $('#docx-input-reupload');
const docxInput = $('#docx-input');
const anyInput = $('#any-input');
const selectBtn = $('#select-btn');
const selectExcelBtn = $('#select-excel-btn');
const selectDocxBtn = $('#select-docx-btn');
const fileNameLabel = $('#file-name-label');
const dropZone = $('#drop-zone');
const uploadSettings = $('#upload-settings');
const deviceSelector = $('#device-selector');
const providerSelector = $('#provider-selector');
const apiProviderSelect = $('#api-provider');
const deviceBadge = $('#device-badge');
const chipInternalGpu = $('#chip-internal-gpu');
const internalGpuInfo = $('#internal-gpu-info');
const internalGpuLabel = $('#internal-gpu-label');
const btnTestInternal = $('#btn-test-internal');
const internalHealthResult = $('#internal-health-result');
const colabSelector = $('#colab-selector');
const colabUrlInput = $('#colab-url');
const colabTokenInput = $('#colab-token');
const btnTestColab = $('#btn-test-colab');
const colabHealthResult = $('#colab-health-result');

const viewUpload = $('#view-upload');
const viewProcessing = $('#view-processing');
const viewReview = $('#view-review');
const viewSuccess = $('#view-success');

const progressFill = $('#progress-fill');
const progressText = $('#progress-text');
const progressPages = $('#progress-pages');
const processingTitle = $('#processing-title');
const processingSubtitle = $('#processing-subtitle');
const processingSpinner = $('#processing-spinner');
const pageStatusGrid = $('#page-status-grid');
const logConsole = $('#log-console');
const excelCompletePanel = $('#excel-complete-panel');
const partialExcelPanel = $('#partial-excel-panel');
const pageExportList = $('#page-export-list');
const pageExportSelectAll = $('#page-export-select-all');
const btnDownloadPagesExcel = $('#btn-download-pages-excel');
const btnDownloadPagesDocx = $('#btn-download-pages-docx');
const pageExportHint = $('#page-export-hint');
const btnDownloadExcel = $('#btn-download-excel');
const btnDownloadDocx = $('#btn-download-docx');
const btnReuploadExcel = $('#btn-reupload-excel');
const btnReuploadDocx = $('#btn-reupload-docx');

const successBatchCode = $('#success-batch-code');
const successTotalRecords = $('#success-total-records');
const btnSuccessNew = $('#btn-success-new');
const btnSuccessHome = $('#btn-success-home');
const btnReviewBack = $('#btn-review-back');
const btnCreateBatch = $('#btn-create-batch');

const stepItems = $$('.step-item');
const notificationCenter = $('#notification-center');

// ── Notifications (toast + lịch sử phiên) ──
const NOTIFY_HISTORY_MAX = 40;
/** @type {{id:number,level:string,title:string,message:string,at:string}[]} */
const notifyHistory = [];
let notifySeq = 0;

function defaultNotifyDuration(level) {
    if (level === 'error') return 16000;
    if (level === 'warn') return 14000;
    if (level === 'success') return 9000;
    return 10000;
}

function notify(level, title, message = '', durationMs) {
    const ms = durationMs == null ? defaultNotifyDuration(level) : durationMs;
    const entry = {
        id: ++notifySeq,
        level,
        title: String(title || ''),
        message: String(message || ''),
        at: new Date().toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    };
    notifyHistory.unshift(entry);
    if (notifyHistory.length > NOTIFY_HISTORY_MAX) notifyHistory.length = NOTIFY_HISTORY_MAX;
    updateNotifyLogUi();

    if (!notificationCenter) return;
    const el = document.createElement('div');
    el.className = `toast toast-${level}`;
    el.dataset.notifyId = String(entry.id);
    el.innerHTML = `
        <div class="toast-top">
            <strong>${escapeHtml(entry.title)}</strong>
            <button type="button" class="toast-close" aria-label="Đóng">×</button>
        </div>
        ${entry.message ? `<p>${escapeHtml(entry.message)}</p>` : ''}
        <button type="button" class="toast-open-log">Xem lại</button>`;
    notificationCenter.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));

    const dismiss = () => {
        el.classList.remove('show');
        setTimeout(() => el.remove(), 300);
    };
    el.querySelector('.toast-close')?.addEventListener('click', (e) => {
        e.stopPropagation();
        dismiss();
    });
    el.querySelector('.toast-open-log')?.addEventListener('click', (e) => {
        e.stopPropagation();
        openNotifyLog();
    });
    setTimeout(dismiss, ms);
}

function updateNotifyLogUi() {
    const badge = document.getElementById('notify-log-badge');
    const list = document.getElementById('notify-log-list');
    if (badge) {
        const n = notifyHistory.length;
        badge.textContent = String(n);
        badge.classList.toggle('hidden', n === 0);
    }
    if (!list) return;
    if (!notifyHistory.length) {
        list.innerHTML = '<p class="notify-log-empty text-muted">Chưa có thông báo.</p>';
        return;
    }
    list.innerHTML = notifyHistory.map((e) => `
        <article class="notify-log-item notify-log-${escapeAttr(e.level)}">
            <div class="notify-log-item-top">
                <strong>${escapeHtml(e.title)}</strong>
                <time>${escapeHtml(e.at)}</time>
            </div>
            ${e.message ? `<p>${escapeHtml(e.message)}</p>` : ''}
        </article>`).join('');
}

function openNotifyLog() {
    const panel = document.getElementById('notify-log-panel');
    const btn = document.getElementById('btn-notify-log');
    if (!panel) return;
    panel.classList.remove('hidden');
    btn?.setAttribute('aria-expanded', 'true');
    updateNotifyLogUi();
}

function closeNotifyLog() {
    const panel = document.getElementById('notify-log-panel');
    const btn = document.getElementById('btn-notify-log');
    panel?.classList.add('hidden');
    btn?.setAttribute('aria-expanded', 'false');
}

function setupNotifyLog() {
    document.getElementById('btn-notify-log')?.addEventListener('click', () => {
        const panel = document.getElementById('notify-log-panel');
        if (panel?.classList.contains('hidden')) openNotifyLog();
        else closeNotifyLog();
    });
    document.getElementById('btn-notify-log-close')?.addEventListener('click', () => closeNotifyLog());
    document.getElementById('btn-notify-log-clear')?.addEventListener('click', () => {
        notifyHistory.length = 0;
        updateNotifyLogUi();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeNotifyLog();
    });
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = String(str ?? '');
    return d.innerHTML;
}

function escapeAttr(str) {
    return String(str ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// ââ Init ââ
document.addEventListener('DOMContentLoaded', async () => {
    setupNotifyLog();
    await loadEnvironmentProfiles();
    if (activeEnvId === 'prod' && !prodKeycloakReady) {
        activeEnvId = 'dev';
        localStorage.setItem(ENV_STORAGE_KEY, 'dev');
    }
    updateEnvUi();
    document.getElementById('btn-env-switch')?.addEventListener('click', () => switchEnvironment());
    await loadRuntimeConfig();
    await loadFieldConfig();
    await loadTemplates();
    setupUpload();
    setupProcessing();
    setupReviewNav();
    setupSuccessButtons();
    setupReviewPage();
    setupSuccessPage();
    setupTemplateUi();
    syncModeUi();
});

async function loadRuntimeConfig() {
    try {
        const res = await fetch(`${getApiBase()}/api/ocr/config`);
        if (res.ok) {
            runtimeConfig = await res.json();
            if (runtimeConfig.internal_gpu_configured) {
                internalGpuLabel.textContent = runtimeConfig.internal_gpu_label
                    ? `MÃ¡y chá»§ GPU: ${runtimeConfig.internal_gpu_label}`
                    : 'MÃ¡y chá»§ GPU ná»i bá»';
            } else {
                chipInternalGpu.classList.add('disabled');
                chipInternalGpu.querySelector('input').disabled = true;
            }
        }
    } catch {
        runtimeConfig = null;
    }

    const savedColabUrl = localStorage.getItem('colab_url');
    const savedColabToken = localStorage.getItem('colab_token');
    if (savedColabUrl) colabUrlInput.value = savedColabUrl;
    if (savedColabToken) colabTokenInput.value = savedColabToken;

    const savedMode = localStorage.getItem('processing_mode');
    if (savedMode) {
        const radio = document.querySelector(`input[name="processing-mode"][value="${savedMode}"]`);
        if (radio && !radio.disabled) radio.checked = true;
    } else if (runtimeConfig?.internal_gpu_configured) {
        const radio = document.querySelector('input[name="processing-mode"][value="remote-internal"]');
        if (radio && !radio.disabled) radio.checked = true;
    }
    syncModeUi();
}

function setStep(step) {
    currentStep = step;
    stepItems.forEach((el, i) => {
        el.classList.remove('active', 'done');
        if (i === step) el.classList.add('active');
        else if (i < step) el.classList.add('done');
    });
    [viewUpload, viewProcessing, viewReview, viewSuccess].forEach(v => v?.classList.remove('active'));
    if (step === 0) viewUpload?.classList.add('active');
    else if (step === 1) {
        viewProcessing?.classList.add('active');
        hideReviewPage?.();
    } else if (step === 2) viewReview?.classList.add('active');
    else if (step === 3) viewSuccess?.classList.add('active');
}

function resetAll() {
    stopPolling();
    selectedFile = null;
    jobId = '';
    totalPages = 0;
    ocrData = null;
    jobStatus = null;
    lastLogCount = 0;
    uploadSource = 'pdf';
    if (fileInput) fileInput.value = '';
    if (excelInput) excelInput.value = '';
    if (docxInput) docxInput.value = '';
    if (excelInputReupload) excelInputReupload.value = '';
    if (fileNameLabel) fileNameLabel.textContent = 'ChÆ°a chá»n file';
    if (logConsole) logConsole.innerHTML = '';
    if (pageStatusGrid) pageStatusGrid.innerHTML = '';
    excelCompletePanel?.classList.add('hidden');
    partialExcelPanel?.classList.add('hidden');
    selectedExportPages.clear();
    if (pageExportList) pageExportList.innerHTML = '';
    if (pageExportSelectAll) pageExportSelectAll.checked = false;
    if (btnDownloadPagesExcel) btnDownloadPagesExcel.disabled = true;
    if (btnDownloadPagesDocx) btnDownloadPagesDocx.disabled = true;
    processingSpinner?.classList.remove('hidden');
    uploadSettings?.classList.remove('hidden');
    hideReviewPage?.();
    hideSuccessPage?.();
    setStep(0);
}

function setupUpload() {
    selectBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        uploadSource = 'pdf';
        uploadSettings?.classList.remove('hidden');
        fileInput?.click();
    });
    selectExcelBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        uploadSource = 'excel';
        uploadSettings?.classList.add('hidden');
        excelInput?.click();
    });
    selectDocxBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        uploadSource = 'docx';
        uploadSettings?.classList.add('hidden');
        docxInput?.click();
    });

    fileInput?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            fileNameLabel.textContent = e.target.files[0].name;
            handleFile(e.target.files[0]);
        }
    });
    excelInput?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            fileNameLabel.textContent = e.target.files[0].name;
            handleExcelFile(e.target.files[0], '');
            e.target.value = '';
        }
    });
    docxInput?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            fileNameLabel.textContent = e.target.files[0].name;
            handleDocxFile(e.target.files[0], '');
            e.target.value = '';
        }
    });

    dropZone?.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });
    dropZone?.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
    });
    dropZone?.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        if (!e.dataTransfer.files.length) return;
        routeUploadFile(e.dataTransfer.files[0]);
    });
    dropZone?.addEventListener('click', () => anyInput?.click());
    anyInput?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            routeUploadFile(e.target.files[0]);
            e.target.value = '';
        }
    });

    $$('input[name="processing-mode"]').forEach(r => r.addEventListener('change', () => {
        localStorage.setItem('processing_mode', getProcessingMode());
        syncModeUi();
    }));
    btnTestInternal?.addEventListener('click', (e) => { e.preventDefault(); testWorker('internal'); });
    btnTestColab?.addEventListener('click', (e) => { e.preventDefault(); testWorker('colab'); });
    document.getElementById('btn-clear-file')?.addEventListener('click', (e) => {
        e.preventDefault();
        if (confirm('XÃ³a file vÃ  lÃ m láº¡i tá»« Äáº§u?')) resetAll();
    });
}

function setupProcessing() {
    btnDownloadExcel?.addEventListener('click', () => {
        if (!jobId) return;
        downloadExcelPages(null);
    });
    btnDownloadDocx?.addEventListener('click', () => {
        if (!jobId) return;
        downloadDocxPages(null);
    });
    btnDownloadPagesExcel?.addEventListener('click', () => {
        const pages = Array.from(selectedExportPages).sort((a, b) => a - b);
        if (!pages.length) {
            notify('warn', 'ChÆ°a chá»n trang', 'Tick Ã­t nháº¥t má»t trang ÄÃ£ OCR xong.');
            return;
        }
        downloadExcelPages(pages);
    });
    btnDownloadPagesDocx?.addEventListener('click', () => {
        const pages = Array.from(selectedExportPages).sort((a, b) => a - b);
        if (!pages.length) {
            notify('warn', 'ChÆ°a chá»n trang', 'Tick Ã­t nháº¥t má»t trang ÄÃ£ xong.');
            return;
        }
        downloadDocxPages(pages);
    });
    pageExportSelectAll?.addEventListener('change', (e) => {
        const checked = e.target.checked;
        pageExportList?.querySelectorAll('.page-export-cb:not(:disabled)').forEach(cb => {
            cb.checked = checked;
            const pn = +cb.dataset.page;
            if (checked) selectedExportPages.add(pn);
            else selectedExportPages.delete(pn);
        });
        syncPageExportDownloadBtn();
    });
    btnReuploadExcel?.addEventListener('click', () => excelInputReupload?.click());
    btnReuploadDocx?.addEventListener('click', () => docxInputReupload?.click());
    excelInputReupload?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            handleExcelFile(e.target.files[0], jobId);
            e.target.value = '';
        }
    });
    docxInputReupload?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            handleDocxFile(e.target.files[0], jobId);
            e.target.value = '';
        }
    });
}

function setupReviewNav() {
    btnReviewBack?.addEventListener('click', () => {
        if (uploadSource === 'excel' || uploadSource === 'docx') setStep(0);
        else setStep(1);
    });
    btnCreateBatch?.addEventListener('click', () => submitBatch());
}

function setupSuccessButtons() {
    btnSuccessNew?.addEventListener('click', () => resetAll());
    btnSuccessHome?.addEventListener('click', () => resetAll());
}

function getModeKind() {
    const mode = getProcessingMode();
    if (mode === 'remote-internal') return { processing_mode: 'remote', remote_provider: 'internal' };
    if (mode === 'remote-colab') return { processing_mode: 'remote', remote_provider: 'colab' };
    return { processing_mode: mode, remote_provider: '' };
}

async function testWorker(provider) {
    const resultEl = provider === 'internal' ? internalHealthResult : colabHealthResult;
    resultEl.classList.remove('hidden', 'ok', 'err');
    resultEl.textContent = 'Äang kiá»m tra...';
    const params = new URLSearchParams({ provider });
    if (provider === 'colab') {
        params.set('url', colabUrlInput.value.trim());
        params.set('token', colabTokenInput.value.trim());
    }
    try {
        const res = await fetch(`${getApiBase()}/api/ocr/worker/health?${params}`);
        const data = await res.json();
        if (data.reachable && data.status === 'healthy') {
            resultEl.classList.add('ok');
            resultEl.textContent = `â Worker online${data.use_gpu ? ' (GPU)' : ''}`;
        } else {
            resultEl.classList.add('err');
            resultEl.textContent = `â ${data.detail || data.status || 'KhÃ´ng káº¿t ná»i ÄÆ°á»£c'}`;
        }
    } catch {
        resultEl.classList.add('err');
        resultEl.textContent = 'â Lá»i káº¿t ná»i tá»i backend';
    }
}

function syncModeUi() {
    const mode = getProcessingMode();
    const isLocal = mode === 'local';
    const isInternal = mode === 'remote-internal';
    const isColab = mode === 'remote-colab';
    const isApi = mode === 'api';
    const gpuAvailable = runtimeConfig?.local_gpu_available === true;

    deviceSelector?.classList.toggle('hidden', !isLocal);
    internalGpuInfo?.classList.toggle('hidden', !isInternal);
    colabSelector?.classList.toggle('hidden', !isColab);
    providerSelector?.classList.toggle('hidden', !isApi);

    const gpuRadio = document.querySelector('input[name="use-gpu"][value="true"]');
    const gpuChip = gpuRadio?.closest('.radio-chip');
    if (gpuRadio && gpuChip) {
        gpuRadio.disabled = !gpuAvailable;
        gpuChip.classList.toggle('disabled', !gpuAvailable);
        if (!gpuAvailable && gpuRadio.checked) {
            const cpuRadio = document.querySelector('input[name="use-gpu"][value="false"]');
            if (cpuRadio) cpuRadio.checked = true;
        }
    }

    if (isLocal) deviceBadge.textContent = getUseGpu() ? 'Local GPU' : 'Local CPU';
    else if (isInternal) deviceBadge.textContent = 'GPU ná»i bá»';
    else if (isColab) deviceBadge.textContent = 'Colab GPU';
    else if (isApi) deviceBadge.textContent = 'API';
    else deviceBadge.textContent = mode.toUpperCase();
}

function getProcessingMode() {
    const checked = document.querySelector('input[name="processing-mode"]:checked');
    return checked ? checked.value : 'local';
}

function getUseGpu() {
    const checked = document.querySelector('input[name="use-gpu"]:checked');
    return checked ? checked.value === 'true' : false;
}

function handleFile(file) {
    if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
        notify('error', 'Chá» há» trá»£ file PDF');
        return;
    }
    selectedFile = file;
    uploadSource = 'pdf';
    lastLogCount = 0;
    logConsole.innerHTML = '';
    pageStatusGrid.innerHTML = '';
    excelCompletePanel?.classList.add('hidden');
    partialExcelPanel?.classList.add('hidden');
    selectedExportPages.clear();
    setStep(1);
    uploadPdf(file);
}

function handleExcelFile(file, targetJobId = '') {
    const name = (file?.name || '').toLowerCase();
    if (!(name.endsWith('.xlsx') || name.endsWith('.xlsm'))) {
        notify('error', 'Chá» há» trá»£ Excel .xlsx/.xlsm');
        return;
    }
    uploadExcel(file, targetJobId);
}

function handleDocxFile(file, targetJobId = '') {
    const name = (file?.name || '').toLowerCase();
    if (!name.endsWith('.docx')) {
        notify('error', 'Chá» há» trá»£ Word .docx');
        return;
    }
    uploadDocx(file, targetJobId);
}

function routeUploadFile(file) {
    if (!file) return;
    const name = (file.name || '').toLowerCase();
    if (fileNameLabel) fileNameLabel.textContent = file.name;
    if (name.endsWith('.pdf')) {
        uploadSource = 'pdf';
        uploadSettings?.classList.remove('hidden');
        handleFile(file);
    } else if (/\.(xlsx|xlsm)$/i.test(name)) {
        uploadSource = 'excel';
        uploadSettings?.classList.add('hidden');
        handleExcelFile(file, '');
    } else if (name.endsWith('.docx')) {
        uploadSource = 'docx';
        uploadSettings?.classList.add('hidden');
        handleDocxFile(file, '');
    } else {
        notify('error', 'Äá»nh dáº¡ng khÃ´ng há» trá»£', 'Chá» cháº¥p nháº­n PDF, Word (.docx) hoáº·c Excel (.xlsx/.xlsm)');
    }
}

async function uploadPdf(file) {
    updateProgress(2, 'Äang upload file...');
    const modeKind = getModeKind();

    if (modeKind.processing_mode === 'remote' && modeKind.remote_provider === 'colab') {
        const url = colabUrlInput.value.trim();
        if (!url) {
            notify('error', 'Thiáº¿u URL Colab', 'Vui lÃ²ng nháº­p URL tunnel Colab.');
            setStep(0);
            return;
        }
        localStorage.setItem('colab_url', url);
        localStorage.setItem('colab_token', colabTokenInput.value.trim());
    }

    try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('processing_mode', modeKind.processing_mode);
        formData.append('use_gpu', getUseGpu() ? 'true' : 'false');
        formData.append('template_id', getSelectedTemplateId());
        if (modeKind.remote_provider) formData.append('remote_provider', modeKind.remote_provider);
        if (modeKind.remote_provider === 'colab') {
            formData.append('remote_url', colabUrlInput.value.trim());
            formData.append('remote_token', colabTokenInput.value.trim());
        }
        if (modeKind.processing_mode === 'api') {
            formData.append('api_provider', apiProviderSelect.value || 'ocrspace');
        }

        const res = await fetch(`${getApiBase()}/api/ocr/upload`, { method: 'POST', body: formData });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Upload failed');
        }

        const data = await res.json();
        jobId = data.job_id;
        uploadSource = 'pdf';

        let subtitle = `Cháº¿ Äá» ${data.processing_mode.toUpperCase()}`;
        if (data.remote_provider) subtitle += ` Â· ${data.remote_provider}`;
        if (data.use_gpu) subtitle += ' Â· GPU';
        processingSubtitle.textContent = subtitle;

        appendLog({ level: 'info', message: `Upload thÃ nh cÃ´ng â Job ${jobId}`, timestamp: new Date().toISOString() });
        if (data.queue_position > 1) {
            appendLog({ level: 'info', message: `Äang xáº¿p hÃ ng GPU â vá» trÃ­ ${data.queue_position}`, timestamp: new Date().toISOString() });
        }
        startPolling();
    } catch (e) {
        notify('error', 'Lá»i upload PDF', e.message || 'Kiá»m tra backend port 8100');
        setStep(0);
    }
}

async function uploadExcel(file, targetJobId = '') {
    const isReupload = !!targetJobId;
    notify('info', isReupload ? 'Äang náº¡p Excel ÄÃ£ sá»­a...' : 'Äang náº¡p Excel...', file.name);

    try {
        const formData = new FormData();
        formData.append('file', file);
        if (targetJobId) formData.append('job_id', targetJobId);
        if (!targetJobId) formData.append('template_id', getSelectedTemplateId());

        const res = await fetch(`${getApiBase()}/api/ocr/upload-excel`, { method: 'POST', body: formData });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload Excel tháº¥t báº¡i');

        jobId = data.job_id;
        stopPolling();
        fileNameLabel.textContent = file.name;

        if (!isReupload) {
            uploadSource = 'excel';
            const warns = Array.isArray(data.warnings) ? data.warnings : [];
            if (warns.length) {
                notify('warn', 'Nạp Excel — có cảnh báo', warns.slice(0, 4).join('\n') + (warns.length > 4 ? '\n…' : ''), 12000);
            } else {
                notify('success', 'Nạp Excel thành công', `Job ${jobId} — bỏ qua OCR, vào bước kiểm tra.`);
            }
        } else {
            const warns = Array.isArray(data.warnings) ? data.warnings : [];
            if (warns.length) {
                notify('warn', 'Đã cập nhật Excel — cảnh báo', warns.slice(0, 4).join('\n'), 12000);
            } else {
                notify('success', 'Đã cập nhật từ Excel', 'Dữ liệu đã ghi đè, đang tải lại review.');
            }
        }

        await submitReview();
    } catch (e) {
        notify('error', 'Lá»i náº¡p Excel', e.message || 'KhÃ´ng xÃ¡c Äá»nh');
        if (!isReupload) setStep(0);
    }
}

async function uploadDocx(file, targetJobId = '') {
    const isReupload = !!targetJobId;
    notify('info', isReupload ? 'Äang náº¡p Word ÄÃ£ sá»­a...' : 'Äang náº¡p Word...', file.name);

    try {
        const formData = new FormData();
        formData.append('file', file);
        if (targetJobId) formData.append('job_id', targetJobId);
        if (!targetJobId) formData.append('template_id', getSelectedTemplateId());

        const res = await fetch(`${getApiBase()}/api/ocr/upload-docx`, { method: 'POST', body: formData });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload Word tháº¥t báº¡i');

        jobId = data.job_id;
        stopPolling();
        fileNameLabel.textContent = file.name;

        if (!isReupload) {
            uploadSource = 'docx';
            notify('success', 'Náº¡p Word thÃ nh cÃ´ng', `Job ${jobId} â bá» qua OCR, vÃ o bÆ°á»c kiá»m tra.`);
        } else {
            notify('success', 'ÄÃ£ cáº­p nháº­t tá»« Word', 'Dá»¯ liá»u ÄÃ£ ghi ÄÃ¨, Äang táº£i láº¡i review.');
        }

        await submitReview();
    } catch (e) {
        notify('error', 'Lá»i náº¡p Word', e.message || 'KhÃ´ng xÃ¡c Äá»nh');
        if (!isReupload) setStep(0);
    }
}

function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollTick, 1000);
    pollTick();
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollTick() {
    try {
        const statusRes = await fetch(`${getApiBase()}/api/ocr/status/${jobId}`);
        if (!statusRes.ok) throw new Error('Status failed');
        jobStatus = await statusRes.json();
        totalPages = jobStatus.total_pages;

        renderLogs(jobStatus.logs || []);
        renderPageStatusGrid(jobStatus.page_statuses || []);
        updateProgressFromJob(jobStatus);

        if (jobStatus.status === 'completed') {
            stopPolling();
            updateProgress(100, 'HoÃ n táº¥t!');
            processingTitle.textContent = 'OCR hoÃ n táº¥t';
            processingSubtitle.textContent = `ÄÃ£ xá»­ lÃ½ ${totalPages} trang â táº£i Excel Äá» sá»­a`;
            processingSpinner?.classList.add('hidden');
            partialExcelPanel?.classList.add('hidden');
            excelCompletePanel?.classList.remove('hidden');
            notify('success', 'OCR hoÃ n táº¥t', 'Táº£i file Excel, sá»­a dá»¯ liá»u rá»i upload láº¡i.');
        } else if (jobStatus.status === 'failed') {
            stopPolling();
            notify('error', 'Lá»i OCR', jobStatus.error_message || 'KhÃ´ng xÃ¡c Äá»nh');
            setStep(0);
        }
    } catch {
        stopPolling();
        notify('error', 'Máº¥t káº¿t ná»i', 'KhÃ´ng thá» káº¿t ná»i server OCR.');
        setStep(0);
    }
}

function updateProgressFromJob(job) {
    if (job.status === 'queued') {
        updateProgress(0, `Äang chá» GPU (hÃ ng Äá»£i #${job.queue_position || '?'})â¦`);
        return;
    }
    if (job.status === 'pending') {
        updateProgress(0, 'Äang khá»i táº¡oâ¦');
        return;
    }
    const total = job.total_pages || 0;
    const statuses = job.page_statuses || [];
    const completed = statuses.filter(p => p.status === 'completed').length;
    const processingPage = statuses.find(p => p.status === 'processing');
    const hasProcessing = !!processingPage;
    const effective = completed + (hasProcessing ? 0.5 : 0);
    const pct = job.status === 'completed'
        ? 100
        : total > 0
            ? Math.min(99, Math.round((effective / total) * 100))
            : 5;
    progressFill.style.width = `${pct}%`;
    progressText.textContent = `${pct}%`;
    progressPages.textContent = `${completed} / ${total || 'â¦'} trang`;
    const activePage = processingPage?.page_number
        || (completed < total ? completed + 1 : total);
    processingTitle.textContent = job.status === 'completed'
        ? 'OCR hoÃ n táº¥t'
        : `Äang OCR trang ${activePage}/${total || 'â¦'}`;
}

function updateProgress(pct, text) {
    progressFill.style.width = `${pct}%`;
    progressText.textContent = `${pct}%`;
    if (text) processingSubtitle.textContent = text;
}

function renderLogs(logs) {
    if (logs.length <= lastLogCount) return;
    logs.slice(lastLogCount).forEach(entry => appendLog(entry));
    lastLogCount = logs.length;
}

function appendLog(entry) {
    const div = document.createElement('div');
    div.className = `log-entry ${entry.level || 'info'}`;
    const time = entry.timestamp
        ? new Date(entry.timestamp).toLocaleTimeString('vi-VN')
        : new Date().toLocaleTimeString('vi-VN');
    div.innerHTML = `<span class="log-time">${time}</span><span class="log-msg">${escapeHtml(entry.message)}</span>`;
    logConsole.appendChild(div);
    logConsole.scrollTop = logConsole.scrollHeight;
}

function downloadExcelPages(pages) {
    if (!jobId) return;
    let url = `${getApiBase()}/api/ocr/result/${jobId}/export`;
    if (pages?.length) {
        url += `?pages=${pages.join(',')}`;
    }
    window.open(url);
    const label = pages?.length
        ? `trang ${pages.join(', ')}`
        : 'toÃ n bá»';
    notify('info', 'Äang táº£i Excel', `Xuáº¥t ${label} â má» file vÃ  sá»­a trÆ°á»c khi upload láº¡i.`);
}

function downloadDocxPages(pages) {
    if (!jobId) return;
    let url = `${getApiBase()}/api/ocr/result/${jobId}/export-docx`;
    if (pages?.length) {
        url += `?pages=${pages.join(',')}`;
    }
    window.open(url);
    const label = pages?.length
        ? `trang ${pages.join(', ')}`
        : 'toÃ n bá»';
    notify('info', 'Äang táº£i Word', `Xuáº¥t ${label} â má» file .docx vÃ  sá»­a hoáº·c náº¡p láº¡i qua Náº¡p Word.`);
}

function syncPageExportDownloadBtn() {
    const count = selectedExportPages.size;
    if (btnDownloadPagesExcel) btnDownloadPagesExcel.disabled = count === 0;
    if (btnDownloadPagesDocx) btnDownloadPagesDocx.disabled = count === 0;
    if (pageExportHint) {
        pageExportHint.textContent = count
            ? `ÄÃ£ chá»n ${count} trang`
            : 'Chá»n trang ÄÃ£ OCR xong bÃªn trÃªn';
    }
}

function renderPageExportPanel(pageStatuses) {
    if (!partialExcelPanel || !pageExportList || uploadSource !== 'pdf') return;

    const completed = pageStatuses.filter(p => p.status === 'completed');
    const show = (totalPages > 1 || completed.length > 0)
        && pageStatuses.some(p => p.status === 'completed' || p.status === 'processing');

    if (!show) {
        partialExcelPanel.classList.add('hidden');
        return;
    }

    partialExcelPanel.classList.remove('hidden');

    // Prune selections for pages no longer completed
    const completedNums = new Set(completed.map(p => p.page_number));
    for (const pn of [...selectedExportPages]) {
        if (!completedNums.has(pn)) selectedExportPages.delete(pn);
    }

    pageExportList.innerHTML = pageStatuses.map(ps => {
        const done = ps.status === 'completed';
        const processing = ps.status === 'processing';
        const failed = ps.status === 'failed';
        const checked = done && selectedExportPages.has(ps.page_number);
        const statusLabel = done ? 'ÄÃ£ xong' : processing ? 'Äang OCR' : failed ? 'Lá»i' : 'Chá»';
        const cls = done ? 'page-export-item done' : processing ? 'page-export-item processing' : 'page-export-item';
        return `<label class="${cls}">
            <input type="checkbox" class="page-export-cb" data-page="${ps.page_number}"
                ${done ? '' : 'disabled'} ${checked ? 'checked' : ''}>
            <span class="page-export-label">Trang ${ps.page_number}</span>
            <span class="page-export-status">${statusLabel}</span>
        </label>`;
    }).join('');

    pageExportList.querySelectorAll('.page-export-cb').forEach(cb => {
        cb.addEventListener('change', () => {
            const pn = +cb.dataset.page;
            if (cb.checked) selectedExportPages.add(pn);
            else selectedExportPages.delete(pn);
            if (pageExportSelectAll) {
                const enabled = pageExportList.querySelectorAll('.page-export-cb:not(:disabled)');
                const allChecked = enabled.length && [...enabled].every(c => c.checked);
                pageExportSelectAll.checked = allChecked;
            }
            syncPageExportDownloadBtn();
        });
    });

    if (pageExportSelectAll) {
        const enabled = pageExportList.querySelectorAll('.page-export-cb:not(:disabled)');
        pageExportSelectAll.checked = enabled.length > 0
            && [...enabled].every(c => c.checked);
    }
    syncPageExportDownloadBtn();
}

function renderPageStatusGrid(pageStatuses) {
    if (!pageStatuses.length) return;
    pageStatusGrid.innerHTML = pageStatuses.map(ps => {
        const icons = { pending: 'o', processing: '...', completed: 'OK', failed: 'X' };
        return `<div class="page-chip ${ps.status}">
            <span class="chip-dot"></span> Trang ${ps.page_number} ${icons[ps.status] || ''}
        </div>`;
    }).join('');
    renderPageExportPanel(pageStatuses);
}

/* ============================================================
   Template profiles (multi-system)
   ============================================================ */

const TEMPLATE_STORAGE_KEY = 'ocr_selected_template_id';
let templatesCache = [];
let availableFieldsCache = [];
let availableActionsCache = [];
let editingTemplate = null;

function getSelectedTemplateId() {
    const sel = document.getElementById('template-select');
    return (sel?.value || localStorage.getItem(TEMPLATE_STORAGE_KEY) || 'sso-agribank').trim();
}

async function loadTemplates() {
    try {
        const [tplRes, fieldsRes, actionsRes] = await Promise.all([
            fetch(`${getApiBase()}/api/templates`),
            fetch(`${getApiBase()}/api/templates/fields`),
            fetch(`${getApiBase()}/api/templates/actions`),
        ]);
        if (tplRes.ok) {
            const data = await tplRes.json();
            templatesCache = data.templates || [];
            populateTemplateSelect(data.default_id || 'sso-agribank');
        }
        if (fieldsRes.ok) {
            availableFieldsCache = (await fieldsRes.json()).fields || [];
        }
        if (actionsRes.ok) {
            availableActionsCache = (await actionsRes.json()).actions || [];
        }
        updateTemplateActionsHint();
    } catch (e) {
        console.warn('loadTemplates failed', e);
    }
}

function populateTemplateSelect(defaultId) {
    const sel = document.getElementById('template-select');
    if (!sel) return;
    const saved = localStorage.getItem(TEMPLATE_STORAGE_KEY) || defaultId;
    sel.innerHTML = templatesCache.map((t) =>
        `<option value="${t.id}">${escapeHtml(t.name || t.id)}</option>`
    ).join('');
    if ([...sel.options].some((o) => o.value === saved)) sel.value = saved;
    else if ([...sel.options].some((o) => o.value === defaultId)) sel.value = defaultId;
}

function updateTemplateActionsHint() {
    const hint = document.getElementById('template-actions-hint');
    if (!hint) return;
    const tpl = templatesCache.find((t) => t.id === getSelectedTemplateId());
    if (!tpl) {
        hint.textContent = '';
        return;
    }
    const actions = (tpl.actions || []).join(', ');
    hint.textContent = 'Actions: ' + (actions || '(không có)');
}

function setupTemplateUi() {
    const sel = document.getElementById('template-select');
    sel?.addEventListener('change', () => {
        localStorage.setItem(TEMPLATE_STORAGE_KEY, sel.value);
        updateTemplateActionsHint();
        const tpl = templatesCache.find((t) => t.id === sel.value);
        if (tpl) showTemplateEditor(tpl);
    });

    document.getElementById('btn-template-config')?.addEventListener('click', (e) => {
        e.preventDefault();
        const panel = document.getElementById('template-config-panel');
        if (panel) panel.open = !panel.open;
        const tpl = templatesCache.find((t) => t.id === getSelectedTemplateId());
        if (tpl) showTemplateEditor(tpl);
    });

    document.getElementById('btn-upload-template-sample')?.addEventListener('click', () => {
        document.getElementById('template-sample-input')?.click();
    });

    document.getElementById('template-sample-input')?.addEventListener('change', async (e) => {
        const file = e.target.files?.[0];
        e.target.value = '';
        if (!file) return;
        await uploadTemplateSample(file);
    });

    document.getElementById('btn-save-template')?.addEventListener('click', async (e) => {
        e.preventDefault();
        await saveEditingTemplate();
    });

    document.getElementById('btn-delete-template')?.addEventListener('click', async (e) => {
        e.preventDefault();
        await deleteEditingTemplate();
    });
}

async function uploadTemplateSample(file) {
    const name = document.getElementById('template-name-input')?.value?.trim() || '';
    const save = document.getElementById('template-save-on-upload')?.checked ? 'true' : 'false';
    const formData = new FormData();
    formData.append('file', file);
    if (name) formData.append('name', name);
    formData.append('save', save);

    try {
        notify('info', 'Đang phân tích file mẫu…', file.name);
        const res = await fetch(`${getApiBase()}/api/templates/upload`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload mẫu thất bại');

        const warnEl = document.getElementById('template-infer-warnings');
        if (warnEl) {
            if (data.warnings?.length) {
                warnEl.classList.remove('hidden');
                warnEl.textContent = data.warnings.join(' · ');
            } else {
                warnEl.classList.add('hidden');
                warnEl.textContent = '';
            }
        }

        showTemplateEditor(data.draft);
        if (save === 'true') {
            await loadTemplates();
            const sel = document.getElementById('template-select');
            if (sel && data.draft?.id) {
                sel.value = data.draft.id;
                localStorage.setItem(TEMPLATE_STORAGE_KEY, data.draft.id);
            }
            notify('success', 'Đã lưu template', data.draft?.name || data.draft?.id);
        } else {
            notify('success', 'Đã suy ra draft', 'Chỉnh mapping rồi bấm Lưu template');
        }
        const panel = document.getElementById('template-config-panel');
        if (panel) panel.open = true;
    } catch (err) {
        notify('error', 'Lỗi template', err.message || String(err));
    }
}

function showTemplateEditor(profile) {
    editingTemplate = JSON.parse(JSON.stringify(profile));
    const editor = document.getElementById('template-editor');
    if (!editor) return;
    editor.classList.remove('hidden');

    const idInput = document.getElementById('tpl-edit-id');
    const nameInput = document.getElementById('tpl-edit-name');
    if (idInput) idInput.value = profile.id || '';
    if (nameInput) nameInput.value = profile.name || '';

    const tbody = document.getElementById('template-map-body');
    if (tbody) {
        const cols = profile.table?.columns || [];
        tbody.innerHTML = cols.map((col, idx) => {
            const opts = availableFieldsCache.map((f) =>
                `<option value="${f.id}" ${f.id === col.field ? 'selected' : ''}>${escapeHtml(f.label || f.id)}</option>`
            ).join('');
            return `<tr data-idx="${idx}">
                <td>${col.index}</td>
                <td><input type="text" class="text-input tpl-header" value="${escapeAttr(col.header || '')}"></td>
                <td><select class="select-input tpl-field"><option value="">—</option>${opts}</select></td>
                <td><input type="checkbox" class="tpl-required" ${col.required ? 'checked' : ''}></td>
            </tr>`;
        }).join('');
    }

    const actionsEl = document.getElementById('template-actions-list');
    if (actionsEl) {
        const enabled = new Set(profile.actions || []);
        actionsEl.innerHTML = availableActionsCache.map((a) =>
            `<label class="check-inline" title="${escapeAttr(a.description || '')}">
                <input type="checkbox" class="tpl-action" data-action="${a.id}" ${enabled.has(a.id) ? 'checked' : ''}>
                ${escapeHtml(a.label || a.id)}
            </label>`
        ).join('');
    }

    const delBtn = document.getElementById('btn-delete-template');
    if (delBtn) delBtn.disabled = !!profile.builtin;
}

function collectEditingTemplate() {
    if (!editingTemplate) return null;
    const name = document.getElementById('tpl-edit-name')?.value?.trim() || editingTemplate.name;
    const rows = document.querySelectorAll('#template-map-body tr');
    const columns = [...rows].map((tr) => {
        const idx = +tr.dataset.idx;
        const prev = editingTemplate.table?.columns?.[idx] || {};
        return {
            index: prev.index ?? idx,
            header: tr.querySelector('.tpl-header')?.value?.trim() || '',
            field: tr.querySelector('.tpl-field')?.value || '',
            required: !!tr.querySelector('.tpl-required')?.checked,
        };
    });
    const actions = [...document.querySelectorAll('.tpl-action:checked')].map((el) => el.dataset.action);
    const excelHeaders = columns.map((c) => c.header);
    return {
        ...editingTemplate,
        name,
        table: { ...(editingTemplate.table || {}), columns },
        actions,
        export: {
            ...(editingTemplate.export || {}),
            excel_headers: excelHeaders,
            docx_title: name,
        },
    };
}

async function saveEditingTemplate() {
    const profile = collectEditingTemplate();
    if (!profile?.id) {
        notify('warn', 'Chưa có template', 'Upload file mẫu trước');
        return;
    }
    try {
        const res = await fetch(`${getApiBase()}/api/templates`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profile),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Lưu thất bại');
        notify('success', 'Đã lưu template', data.name || data.id);
        await loadTemplates();
        const sel = document.getElementById('template-select');
        if (sel) {
            sel.value = data.id;
            localStorage.setItem(TEMPLATE_STORAGE_KEY, data.id);
        }
        showTemplateEditor(data);
        updateTemplateActionsHint();
    } catch (err) {
        notify('error', 'Lỗi lưu template', err.message || String(err));
    }
}

async function deleteEditingTemplate() {
    const id = editingTemplate?.id;
    if (!id || editingTemplate?.builtin) {
        notify('warn', 'Không xóa được', 'Template built-in không thể xóa');
        return;
    }
    if (!confirm('Xóa template "' + id + '"?')) return;
    try {
        const res = await fetch(`${getApiBase()}/api/templates/${id}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Xóa thất bại');
        notify('success', 'Đã xóa template', id);
        editingTemplate = null;
        document.getElementById('template-editor')?.classList.add('hidden');
        localStorage.setItem(TEMPLATE_STORAGE_KEY, 'sso-agribank');
        await loadTemplates();
    } catch (err) {
        notify('error', 'Lỗi xóa template', err.message || String(err));
    }
}

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function escapeAttr(s) {
    return escapeHtml(s).replace(/'/g, '&#39;');
}
