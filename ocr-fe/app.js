/* ============================================================
   Agribank Banca OCR — Client Application (Excel-flow)
   ============================================================ */

const _devFePorts = new Set(['5173', '3000', '5500']);
const ENV_STORAGE_KEY = 'ocr_keycloak_env_v1';
const ENV_META_CACHE_KEY = 'ocr_keycloak_env_meta_v2';
/** Chỉ Vite FE mới trỏ localhost:8100; khi mở từ :8100/LAN/Tailscale → same-origin */
const _viteApiBase = _devFePorts.has(window.location.port) ? 'http://localhost:8100' : '';

let prodKeycloakReady = false;
let prodKeycloakLabel = '';
let activeEnvId = localStorage.getItem(ENV_STORAGE_KEY) || 'dev';

function getActiveEnvId() {
    return activeEnvId;
}

/** Backend OCR — luôn cùng máy đang mở FE (không đổi khi chuyển KC DEV/PROD) */
function getApiBase() {
    return (_viteApiBase || '').replace(/\/$/, '');
}

/** Chỉ áp dụng cho API tạo lô user / Keycloak */
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
    // Xóa cache cũ từng ép api_base=localhost (gây lỗi khi mở qua LAN/Tailscale)
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
        /* giữ cache / mặc định */
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
            ? `Tạo lô user → Keycloak PROD (${prodKeycloakLabel || 'production'})`
            : 'Tạo lô user → Keycloak DEV';
    }
    if (btn) {
        if (!isProd && !prodKeycloakReady) {
            btn.disabled = true;
            btn.textContent = 'PROD chưa cấu hình';
            btn.title = 'Thêm KEYCLOAK_PROD_* trong .env rồi restart server';
        } else {
            btn.disabled = false;
            btn.textContent = isProd ? 'Chuyển KC DEV' : 'Chuyển KC PROD';
            btn.title = isProd
                ? 'Tạo lô sẽ gọi Keycloak DEV'
                : 'Tạo lô sẽ gọi Keycloak PROD (OCR vẫn chạy trên server hiện tại)';
        }
    }
}

async function switchEnvironment() {
    const targetId = activeEnvId === 'dev' ? 'prod' : 'dev';
    if (targetId === 'prod' && !prodKeycloakReady) {
        notify('warning', 'Chưa cấu hình Keycloak PROD', 'Thêm KEYCLOAK_PROD_BASE_URL + KEYCLOAK_PROD_CLIENT_SECRET trong .env.');
        return;
    }
    activeEnvId = targetId;
    localStorage.setItem(ENV_STORAGE_KEY, activeEnvId);
    updateEnvUi();
    // Không gọi lại field-config / không đổi API base — tránh lỗi kết nối BE
    const label = targetId === 'prod' ? 'Keycloak PROD' : 'Keycloak DEV';
    notify('success', `Đã chuyển sang ${label}`, 'OCR không đổi. Chỉ bước Tạo lô user dùng Keycloak mới.', 5000);
}

window.getApiBase = getApiBase;
window.getActiveEnvId = getActiveEnvId;
window.switchEnvironment = switchEnvironment;

// ── State ──
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

// ── DOM refs ──
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

// ── Notifications ──
function notify(level, title, message = '', durationMs = 6000) {
    if (!notificationCenter) return;
    const el = document.createElement('div');
    el.className = `toast toast-${level}`;
    el.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ''}`;
    notificationCenter.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
        el.classList.remove('show');
        setTimeout(() => el.remove(), 300);
    }, durationMs);
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = String(str ?? '');
    return d.innerHTML;
}

function escapeAttr(str) {
    return String(str ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
    await loadEnvironmentProfiles();
    if (activeEnvId === 'prod' && !prodKeycloakReady) {
        activeEnvId = 'dev';
        localStorage.setItem(ENV_STORAGE_KEY, 'dev');
    }
    updateEnvUi();
    document.getElementById('btn-env-switch')?.addEventListener('click', () => switchEnvironment());
    await loadRuntimeConfig();
    await loadFieldConfig();
    setupUpload();
    setupProcessing();
    setupReviewNav();
    setupSuccessButtons();
    setupReviewPage();
    setupSuccessPage();
    syncModeUi();
});

async function loadRuntimeConfig() {
    try {
        const res = await fetch(`${getApiBase()}/api/ocr/config`);
        if (res.ok) {
            runtimeConfig = await res.json();
            if (runtimeConfig.internal_gpu_configured) {
                internalGpuLabel.textContent = runtimeConfig.internal_gpu_label
                    ? `Máy chủ GPU: ${runtimeConfig.internal_gpu_label}`
                    : 'Máy chủ GPU nội bộ';
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
    if (fileNameLabel) fileNameLabel.textContent = 'Chưa chọn file';
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
        if (confirm('Xóa file và làm lại từ đầu?')) resetAll();
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
            notify('warn', 'Chưa chọn trang', 'Tick ít nhất một trang đã OCR xong.');
            return;
        }
        downloadExcelPages(pages);
    });
    btnDownloadPagesDocx?.addEventListener('click', () => {
        const pages = Array.from(selectedExportPages).sort((a, b) => a - b);
        if (!pages.length) {
            notify('warn', 'Chưa chọn trang', 'Tick ít nhất một trang đã xong.');
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
    resultEl.textContent = 'Đang kiểm tra...';
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
            resultEl.textContent = `✓ Worker online${data.use_gpu ? ' (GPU)' : ''}`;
        } else {
            resultEl.classList.add('err');
            resultEl.textContent = `✗ ${data.detail || data.status || 'Không kết nối được'}`;
        }
    } catch {
        resultEl.classList.add('err');
        resultEl.textContent = '✗ Lỗi kết nối tới backend';
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
    else if (isInternal) deviceBadge.textContent = 'GPU nội bộ';
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
        notify('error', 'Chỉ hỗ trợ file PDF');
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
        notify('error', 'Chỉ hỗ trợ Excel .xlsx/.xlsm');
        return;
    }
    uploadExcel(file, targetJobId);
}

function handleDocxFile(file, targetJobId = '') {
    const name = (file?.name || '').toLowerCase();
    if (!name.endsWith('.docx')) {
        notify('error', 'Chỉ hỗ trợ Word .docx');
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
        notify('error', 'Định dạng không hỗ trợ', 'Chỉ chấp nhận PDF, Word (.docx) hoặc Excel (.xlsx/.xlsm)');
    }
}

async function uploadPdf(file) {
    updateProgress(2, 'Đang upload file...');
    const modeKind = getModeKind();

    if (modeKind.processing_mode === 'remote' && modeKind.remote_provider === 'colab') {
        const url = colabUrlInput.value.trim();
        if (!url) {
            notify('error', 'Thiếu URL Colab', 'Vui lòng nhập URL tunnel Colab.');
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

        let subtitle = `Chế độ ${data.processing_mode.toUpperCase()}`;
        if (data.remote_provider) subtitle += ` · ${data.remote_provider}`;
        if (data.use_gpu) subtitle += ' · GPU';
        processingSubtitle.textContent = subtitle;

        appendLog({ level: 'info', message: `Upload thành công — Job ${jobId}`, timestamp: new Date().toISOString() });
        if (data.queue_position > 1) {
            appendLog({ level: 'info', message: `Đang xếp hàng GPU — vị trí ${data.queue_position}`, timestamp: new Date().toISOString() });
        }
        startPolling();
    } catch (e) {
        notify('error', 'Lỗi upload PDF', e.message || 'Kiểm tra backend port 8100');
        setStep(0);
    }
}

async function uploadExcel(file, targetJobId = '') {
    const isReupload = !!targetJobId;
    notify('info', isReupload ? 'Đang nạp Excel đã sửa...' : 'Đang nạp Excel...', file.name);

    try {
        const formData = new FormData();
        formData.append('file', file);
        if (targetJobId) formData.append('job_id', targetJobId);

        const res = await fetch(`${getApiBase()}/api/ocr/upload-excel`, { method: 'POST', body: formData });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload Excel thất bại');

        jobId = data.job_id;
        stopPolling();
        fileNameLabel.textContent = file.name;

        if (!isReupload) {
            uploadSource = 'excel';
            notify('success', 'Nạp Excel thành công', `Job ${jobId} — bỏ qua OCR, vào bước kiểm tra.`);
        } else {
            notify('success', 'Đã cập nhật từ Excel', 'Dữ liệu đã ghi đè, đang tải lại review.');
        }

        await submitReview();
    } catch (e) {
        notify('error', 'Lỗi nạp Excel', e.message || 'Không xác định');
        if (!isReupload) setStep(0);
    }
}

async function uploadDocx(file, targetJobId = '') {
    const isReupload = !!targetJobId;
    notify('info', isReupload ? 'Đang nạp Word đã sửa...' : 'Đang nạp Word...', file.name);

    try {
        const formData = new FormData();
        formData.append('file', file);
        if (targetJobId) formData.append('job_id', targetJobId);

        const res = await fetch(`${getApiBase()}/api/ocr/upload-docx`, { method: 'POST', body: formData });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload Word thất bại');

        jobId = data.job_id;
        stopPolling();
        fileNameLabel.textContent = file.name;

        if (!isReupload) {
            uploadSource = 'docx';
            notify('success', 'Nạp Word thành công', `Job ${jobId} — bỏ qua OCR, vào bước kiểm tra.`);
        } else {
            notify('success', 'Đã cập nhật từ Word', 'Dữ liệu đã ghi đè, đang tải lại review.');
        }

        await submitReview();
    } catch (e) {
        notify('error', 'Lỗi nạp Word', e.message || 'Không xác định');
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
            updateProgress(100, 'Hoàn tất!');
            processingTitle.textContent = 'OCR hoàn tất';
            processingSubtitle.textContent = `Đã xử lý ${totalPages} trang — tải Excel để sửa`;
            processingSpinner?.classList.add('hidden');
            partialExcelPanel?.classList.add('hidden');
            excelCompletePanel?.classList.remove('hidden');
            notify('success', 'OCR hoàn tất', 'Tải file Excel, sửa dữ liệu rồi upload lại.');
        } else if (jobStatus.status === 'failed') {
            stopPolling();
            notify('error', 'Lỗi OCR', jobStatus.error_message || 'Không xác định');
            setStep(0);
        }
    } catch {
        stopPolling();
        notify('error', 'Mất kết nối', 'Không thể kết nối server OCR.');
        setStep(0);
    }
}

function updateProgressFromJob(job) {
    if (job.status === 'queued') {
        updateProgress(0, `Đang chờ GPU (hàng đợi #${job.queue_position || '?'})…`);
        return;
    }
    if (job.status === 'pending') {
        updateProgress(0, 'Đang khởi tạo…');
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
    progressPages.textContent = `${completed} / ${total || '…'} trang`;
    const activePage = processingPage?.page_number
        || (completed < total ? completed + 1 : total);
    processingTitle.textContent = job.status === 'completed'
        ? 'OCR hoàn tất'
        : `Đang OCR trang ${activePage}/${total || '…'}`;
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
        : 'toàn bộ';
    notify('info', 'Đang tải Excel', `Xuất ${label} — mở file và sửa trước khi upload lại.`);
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
        : 'toàn bộ';
    notify('info', 'Đang tải Word', `Xuất ${label} — mở file .docx và sửa hoặc nạp lại qua Nạp Word.`);
}

function syncPageExportDownloadBtn() {
    const count = selectedExportPages.size;
    if (btnDownloadPagesExcel) btnDownloadPagesExcel.disabled = count === 0;
    if (btnDownloadPagesDocx) btnDownloadPagesDocx.disabled = count === 0;
    if (pageExportHint) {
        pageExportHint.textContent = count
            ? `Đã chọn ${count} trang`
            : 'Chọn trang đã OCR xong bên trên';
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
        const statusLabel = done ? 'Đã xong' : processing ? 'Đang OCR' : failed ? 'Lỗi' : 'Chờ';
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
        const icons = { pending: '○', processing: '◉', completed: '✓', failed: '✗' };
        return `<div class="page-chip ${ps.status}">
            <span class="chip-dot"></span> Trang ${ps.page_number} ${icons[ps.status] || ''}
        </div>`;
    }).join('');
    renderPageExportPanel(pageStatuses);
}
