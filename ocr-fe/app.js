/* ============================================================
   Agribank Banca OCR — Client Application
   ============================================================ */

const API_BASE = window.location.port === '8100' ? '' : 'http://localhost:8100';

// ── State ──
let currentStep = 0;
let selectedFile = null;
let jobId = '';
let totalPages = 0;
let currentPageNumber = 1;
let ocrData = null;
let jobStatus = null;
let activeCell = null;
let imageNaturalWidth = 1;
let imageNaturalHeight = 1;
let pollTimer = null;
let lastLogCount = 0;
let workspaceEntered = false;
let runtimeConfig = null;

// ── DOM refs ──
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const fileInput = $('#file-input');
const excelInput = $('#excel-input');
const selectBtn = $('#select-btn');
const selectExcelBtn = $('#select-excel-btn');
const fileNameLabel = $('#file-name-label');
const dropZone = $('#drop-zone');
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
const viewWorkspace = $('#view-workspace');
const viewSuccess = $('#view-success');

const progressFill = $('#progress-fill');
const progressText = $('#progress-text');
const progressPages = $('#progress-pages');
const processingTitle = $('#processing-title');
const processingSubtitle = $('#processing-subtitle');
const pageStatusGrid = $('#page-status-grid');
const logConsole = $('#log-console');
const btnGoReviewEarly = $('#btn-go-review-early');

const processingBanner = $('#processing-banner');
const bannerText = $('#banner-text');
const pageList = $('#page-list');
const sidebarCount = $('#sidebar-count');
const pagePendingOverlay = $('#page-pending-overlay');

const pdfPaneTitle = $('#pdf-pane-title');
const btnPrevPage = $('#btn-prev-page');
const btnNextPage = $('#btn-next-page');
const btnTogglePdf = $('#btn-toggle-pdf');
const btnShowPdf = $('#btn-show-pdf');
const workspaceSplit = $('#workspace-split');
const panePdfWrapper = $('#pane-pdf-wrapper');
const splitResizer = $('#split-resizer');
const pdfPageImage = $('#pdf-page-image');
const bboxContainer = $('#bbox-container');

const btnExportExcel = $('#btn-export-excel');
const btnImportExcel = $('#btn-import-excel');
const btnRestart = $('#btn-restart');
const btnConfirmBatch = $('#btn-confirm-batch');
const alertsSummary = $('#alerts-summary');
const alertSummaryText = $('#alert-summary-text');
const tableScrollerZone = $('#table-scroller-zone');

const successBatchCode = $('#success-batch-code');
const successTotalRecords = $('#success-total-records');
const btnSuccessNew = $('#btn-success-new');
const btnSuccessHome = $('#btn-success-home');

const stepItems = $$('.step-item');

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
    await loadRuntimeConfig();
    setupUpload();
    setupWorkspace();
    setupKeyboard();
    syncModeUi();
});

async function loadRuntimeConfig() {
    try {
        const res = await fetch(`${API_BASE}/api/ocr/config`);
        if (res.ok) {
            runtimeConfig = await res.json();
            if (runtimeConfig.internal_gpu_configured) {
                internalGpuLabel.textContent = `Worker: ${runtimeConfig.internal_gpu_label}`;
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
    }
    syncModeUi();
}

// ── Navigation ──
function setStep(step) {
    currentStep = step;

    stepItems.forEach((el, i) => {
        el.classList.remove('active', 'done');
        if (i === step) el.classList.add('active');
        else if (i < step) el.classList.add('done');
    });

    [viewUpload, viewProcessing, viewWorkspace, viewSuccess].forEach(v => v.classList.remove('active'));
    if (step === 0) viewUpload.classList.add('active');
    else if (step === 1) viewProcessing.classList.add('active');
    else if (step === 2) viewWorkspace.classList.add('active');
    else if (step === 3) viewSuccess.classList.add('active');
}

// ── Upload ──
function setupUpload() {
    // Chỉ nút "Chọn PDF" mở hộp file — không gắn click lên vùng khác
    selectBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        fileInput.click();
    });
    selectExcelBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        excelInput?.click();
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length) {
            fileNameLabel.textContent = e.target.files[0].name;
            handleFile(e.target.files[0]);
        }
    });
    excelInput?.addEventListener('change', (e) => {
        if (e.target.files.length) {
            fileNameLabel.textContent = e.target.files[0].name;
            const targetJob = (currentStep === 2 && jobId) ? jobId : '';
            handleExcelFile(e.target.files[0], targetJob);
            e.target.value = '';
        }
    });

    // Kéo thả chỉ trên dải drop-hint nhỏ
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
    });
    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
    });
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
        if (e.dataTransfer.files.length) {
            fileNameLabel.textContent = e.dataTransfer.files[0].name;
            handleFile(e.dataTransfer.files[0]);
        }
    });

    $$('input[name="processing-mode"]').forEach(r => r.addEventListener('change', () => {
        localStorage.setItem('processing_mode', getProcessingMode());
        syncModeUi();
    }));
    btnGoReviewEarly.addEventListener('click', () => enterWorkspace());
    btnTestInternal?.addEventListener('click', (e) => { e.preventDefault(); testWorker('internal'); });
    btnTestColab?.addEventListener('click', (e) => { e.preventDefault(); testWorker('colab'); });
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
        const res = await fetch(`${API_BASE}/api/ocr/worker/health?${params}`);
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

    deviceSelector.classList.toggle('hidden', !isLocal);
    internalGpuInfo.classList.toggle('hidden', !isInternal);
    colabSelector.classList.toggle('hidden', !isColab);
    providerSelector.classList.toggle('hidden', !isApi);

    const gpuRadio = document.querySelector('input[name="use-gpu"][value="true"]');
    const gpuChip = gpuRadio?.closest('.radio-chip');
    if (gpuRadio && gpuChip) {
        gpuRadio.disabled = !gpuAvailable;
        gpuChip.classList.toggle('disabled', !gpuAvailable);
        gpuChip.title = gpuAvailable
            ? (runtimeConfig?.local_gpu_name || 'GPU sẵn sàng')
            : (runtimeConfig?.local_gpu_detail || 'GPU chưa sẵn sàng — dùng CPU');
        if (!gpuAvailable && gpuRadio.checked) {
            const cpuRadio = document.querySelector('input[name="use-gpu"][value="false"]');
            if (cpuRadio) cpuRadio.checked = true;
        }
    }

    if (isLocal) {
        deviceBadge.textContent = getUseGpu() ? 'Local GPU' : 'Local CPU';
    } else if (isInternal) {
        deviceBadge.textContent = 'GPU nội bộ';
    } else if (isColab) {
        deviceBadge.textContent = 'Colab GPU';
    } else if (isApi) {
        deviceBadge.textContent = 'API';
    } else {
        deviceBadge.textContent = mode.toUpperCase();
    }
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
    if (file.type !== 'application/pdf') {
        alert('Chỉ hỗ trợ file PDF.');
        return;
    }
    selectedFile = file;
    workspaceEntered = false;
    lastLogCount = 0;
    logConsole.innerHTML = '';
    pageStatusGrid.innerHTML = '';
    setStep(1);
    uploadPdf(file);
}

function handleExcelFile(file, targetJobId = '') {
    const name = (file?.name || '').toLowerCase();
    if (!(name.endsWith('.xlsx') || name.endsWith('.xlsm'))) {
        alert('Chỉ hỗ trợ file Excel .xlsx/.xlsm');
        return;
    }
    uploadExcel(file, targetJobId);
}

async function uploadPdf(file) {
    updateProgress(2, 'Đang upload file...');

    const modeKind = getModeKind();

    if (modeKind.processing_mode === 'remote' && modeKind.remote_provider === 'colab') {
        const url = colabUrlInput.value.trim();
        if (!url) {
            alert('Vui lòng nhập URL tunnel Colab.');
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

        if (modeKind.remote_provider) {
            formData.append('remote_provider', modeKind.remote_provider);
        }
        if (modeKind.remote_provider === 'colab') {
            formData.append('remote_url', colabUrlInput.value.trim());
            formData.append('remote_token', colabTokenInput.value.trim());
        }
        if (modeKind.processing_mode === 'api') {
            formData.append('api_provider', apiProviderSelect.value || 'ocrspace');
        }

        const res = await fetch(`${API_BASE}/api/ocr/upload`, { method: 'POST', body: formData });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Upload failed');
        }

        const data = await res.json();
        jobId = data.job_id;

        let subtitle = `Chế độ ${data.processing_mode.toUpperCase()}`;
        if (data.remote_provider) subtitle += ` · ${data.remote_provider}`;
        if (data.use_gpu) subtitle += ' · GPU';
        processingSubtitle.textContent = subtitle;

        appendLog({ level: 'info', message: `Upload thành công — Job ${jobId}`, timestamp: new Date().toISOString() });
        const modeLabel = data.processing_mode === 'remote'
            ? `REMOTE · ${data.remote_provider || 'worker'}`
            : data.processing_mode.toUpperCase();
        appendLog({
            level: 'info',
            message: `Chế độ: ${modeLabel}${data.use_gpu ? ' · GPU' : ' · CPU'}`,
            timestamp: new Date().toISOString(),
        });
        if (data.remote_url) {
            appendLog({ level: 'info', message: `Worker: ${data.remote_url}`, timestamp: new Date().toISOString() });
        }
        startPolling();
    } catch (e) {
        alert(`Lỗi upload: ${e.message || 'Kiểm tra backend port 8100'}`);
        setStep(0);
    }
}

async function uploadExcel(file, targetJobId = '') {
    try {
        const formData = new FormData();
        formData.append('file', file);
        if (targetJobId) formData.append('job_id', targetJobId);

        const res = await fetch(`${API_BASE}/api/ocr/upload-excel`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Upload Excel thất bại');

        jobId = data.job_id;
        stopPolling();
        lastLogCount = 0;
        jobStatus = null;
        const [statusRes, resultRes] = await Promise.all([
            fetch(`${API_BASE}/api/ocr/status/${jobId}`),
            fetch(`${API_BASE}/api/ocr/result/${jobId}`),
        ]);
        if (!statusRes.ok || !resultRes.ok) {
            throw new Error('Không tải được dữ liệu sau khi nạp Excel');
        }
        jobStatus = await statusRes.json();
        ocrData = await resultRes.json();
        totalPages = ocrData.total_pages || ocrData.pages?.length || 1;
        currentPageNumber = ocrData.pages?.[0]?.page_number || 1;
        workspaceEntered = true;
        setStep(2);
        renderWorkspace();
        appendLog({
            level: 'success',
            message: targetJobId
                ? 'Đã nạp Excel và ghi đè dữ liệu OCR hiện tại'
                : 'Đã nạp Excel, bỏ qua OCR',
            timestamp: new Date().toISOString(),
        });
        alert(targetJobId
            ? 'Nạp Excel thành công. Dữ liệu bảng đã được cập nhật.'
            : 'Nạp Excel thành công. Bạn có thể chỉnh sửa ngay.');
    } catch (e) {
        alert(`Lỗi nạp Excel: ${e.message || 'Không xác định'}`);
    }
}

// ── Polling ──
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
        const [statusRes, resultRes] = await Promise.all([
            fetch(`${API_BASE}/api/ocr/status/${jobId}`),
            fetch(`${API_BASE}/api/ocr/result/${jobId}`),
        ]);

        if (!statusRes.ok) throw new Error('Status failed');
        jobStatus = await statusRes.json().catch(() => {
            throw new Error('Phản hồi status không phải JSON — kiểm tra server port 8100');
        });
        totalPages = jobStatus.total_pages;

        renderLogs(jobStatus.logs || []);
        renderPageStatusGrid(jobStatus.page_statuses || []);
        updateProgressFromJob(jobStatus);

        if (resultRes.ok) {
            const partial = await resultRes.json().catch(() => null);
            if (!partial) return;
            ocrData = partial;
            const doneCount = (partial.pages || []).length;

            if (doneCount > 0) {
                btnGoReviewEarly.classList.remove('hidden');
                if (!workspaceEntered && currentStep === 1) {
                    enterWorkspace();
                } else if (workspaceEntered) {
                    refreshWorkspaceFromPoll();
                }
            }
        }

        if (jobStatus.status === 'completed') {
            stopPolling();
            updateProgress(100, 'Hoàn tất!');
            processingTitle.textContent = 'Xử lý hoàn tất';
            processingSubtitle.textContent = `Đã OCR ${totalPages} trang`;
            btnGoReviewEarly.classList.add('hidden');
            processingBanner.classList.add('hidden');
            if (!workspaceEntered) enterWorkspace();
            else refreshWorkspaceFromPoll();
        } else if (jobStatus.status === 'failed') {
            stopPolling();
            alert(`Lỗi OCR: ${jobStatus.error_message}`);
            setStep(0);
        }
    } catch {
        stopPolling();
        alert('Mất kết nối với server OCR.');
        setStep(0);
    }
}

function updateProgressFromJob(job) {
    const pct = job.total_pages > 0
        ? Math.min(99, Math.round((job.progress / job.total_pages) * 100))
        : 5;
    const label = job.status === 'completed' ? 100 : pct;
    progressFill.style.width = `${label}%`;
    progressText.textContent = `${label}%`;
    progressPages.textContent = `${job.progress} / ${job.total_pages || '…'} trang`;
    processingTitle.textContent = `Đang OCR trang ${Math.min(job.progress + 1, job.total_pages || '?')}/${job.total_pages || '…'}`;
}

function updateProgress(pct, text) {
    progressFill.style.width = `${pct}%`;
    progressText.textContent = `${pct}%`;
    if (text) processingSubtitle.textContent = text;
}

// ── Logs ──
function renderLogs(logs) {
    if (logs.length <= lastLogCount) return;
    const newLogs = logs.slice(lastLogCount);
    lastLogCount = logs.length;

    newLogs.forEach(entry => appendLog(entry));
}

function appendLog(entry) {
    const div = document.createElement('div');
    const isRemote = /^\[(Google Colab|GPU nội bộ|Worker)/.test(entry.message || '');
    div.className = `log-entry ${entry.level || 'info'}${isRemote ? ' log-remote' : ''}`;
    const time = entry.timestamp
        ? new Date(entry.timestamp).toLocaleTimeString('vi-VN')
        : new Date().toLocaleTimeString('vi-VN');
    div.innerHTML = `<span class="log-time">${time}</span><span class="log-msg">${escapeHtml(entry.message)}</span>`;
    logConsole.appendChild(div);
    logConsole.scrollTop = logConsole.scrollHeight;
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

// ── Page status grid (processing view) ──
function renderPageStatusGrid(pageStatuses) {
    if (!pageStatuses.length) return;
    pageStatusGrid.innerHTML = pageStatuses.map(ps => {
        const icons = { pending: '○', processing: '◉', completed: '✓', failed: '✗' };
        return `<div class="page-chip ${ps.status}">
            <span class="chip-dot"></span>
            Trang ${ps.page_number} ${icons[ps.status] || ''}
        </div>`;
    }).join('');
}

// ── Workspace entry ──
function enterWorkspace() {
    workspaceEntered = true;
    setStep(2);
    if (!ocrData) return;
    if (currentPageNumber > 1 && !getPageData(currentPageNumber)) {
        currentPageNumber = ocrData.pages[0]?.page_number || 1;
    }
    renderWorkspace();
}

function refreshWorkspaceFromPoll() {
    if (!workspaceEntered || !ocrData) return;

    const isProcessing = jobStatus && jobStatus.status === 'processing';
    processingBanner.classList.toggle('hidden', !isProcessing);
    if (isProcessing) {
        bannerText.textContent = `Đang OCR — ${jobStatus.progress}/${jobStatus.total_pages} trang hoàn tất. Bạn có thể sửa các trang đã xong.`;
    }

    renderPageSidebar();
    sidebarCount.textContent = `${ocrData.pages.length}/${totalPages}`;

    const pageReady = isPageReady(currentPageNumber);
    pagePendingOverlay.classList.toggle('hidden', pageReady);

    if (pageReady) {
        renderTableGrid();
        renderBoundingBoxes();
    } else {
        tableScrollerZone.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-muted)">
            <div class="overlay-spinner" style="margin:0 auto 12px;border-color:var(--red-bg-soft);border-top-color:var(--red-primary)"></div>
            Trang ${currentPageNumber} đang được OCR...<br>
            <small>Vui lòng chuyển sang trang đã hoàn tất để bắt đầu đối chiếu.</small>
        </div>`;
    }
}

function isPageReady(pageNum) {
    if (!ocrData) return false;
    return ocrData.pages.some(p => p.page_number === pageNum);
}

function getPageStatus(pageNum) {
    if (!jobStatus?.page_statuses) return isPageReady(pageNum) ? 'completed' : 'pending';
    const ps = jobStatus.page_statuses.find(p => p.page_number === pageNum);
    return ps ? ps.status : 'pending';
}

function getPageData(pageNum) {
    return ocrData?.pages.find(p => p.page_number === pageNum) || null;
}

// ── Workspace rendering ──
function renderWorkspace() {
    if (!ocrData) return;
    refreshWorkspaceFromPoll();

    pdfPaneTitle.textContent = `Tài liệu gốc (Trang ${currentPageNumber}/${totalPages})`;
    btnPrevPage.disabled = currentPageNumber <= 1;
    btnNextPage.disabled = currentPageNumber >= totalPages;

    const imgUrl = `${API_BASE}/api/ocr/result/${jobId}/page/${currentPageNumber}/image`;
    pdfPageImage.src = imgUrl;
    bboxContainer.innerHTML = '';

    pdfPageImage.onload = () => {
        imageNaturalWidth = pdfPageImage.naturalWidth || 1;
        imageNaturalHeight = pdfPageImage.naturalHeight || 1;
        if (isPageReady(currentPageNumber)) renderBoundingBoxes();
    };

    pagePendingOverlay.classList.toggle('hidden', isPageReady(currentPageNumber));

    if (isPageReady(currentPageNumber)) {
        renderTableGrid();
    } else {
        tableScrollerZone.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-muted)">
            Trang ${currentPageNumber} đang được OCR...
        </div>`;
    }
}

function renderPageSidebar() {
    if (!totalPages) return;
    pageList.innerHTML = '';

    for (let i = 1; i <= totalPages; i++) {
        const li = document.createElement('li');
        li.className = `page-list-item${i === currentPageNumber ? ' active' : ''}`;
        const status = getPageStatus(i);
        const icons = { pending: '○', processing: '◉', completed: '✓', failed: '✗' };
        li.innerHTML = `<span class="status-icon">${icons[status] || '○'}</span> Trang ${i}`;
        li.addEventListener('click', () => {
            currentPageNumber = i;
            activeCell = null;
            renderWorkspace();
        });
        pageList.appendChild(li);
    }
}

function changePage(offset) {
    const next = currentPageNumber + offset;
    if (next >= 1 && next <= totalPages) {
        currentPageNumber = next;
        activeCell = null;
        renderWorkspace();
    }
}

function setupWorkspace() {
    btnPrevPage.addEventListener('click', () => changePage(-1));
    btnNextPage.addEventListener('click', () => changePage(1));
    setupSplitResizer();
    setupPdfPaneToggle();
    btnRestart.addEventListener('click', () => {
        stopPolling();
        fileNameLabel.textContent = 'Chưa chọn file';
        fileInput.value = '';
        setStep(0);
    });
    btnExportExcel.addEventListener('click', () => {
        window.open(`${API_BASE}/api/ocr/result/${jobId}/export`);
    });
    btnImportExcel?.addEventListener('click', (e) => {
        e.preventDefault();
        excelInput?.click();
    });
    btnConfirmBatch.addEventListener('click', submitBatch);
    btnSuccessNew.addEventListener('click', () => { stopPolling(); setStep(0); });
    btnSuccessHome.addEventListener('click', () => { stopPolling(); setStep(0); });
}

// ── Resizable split + PDF pane toggle ──

function setupSplitResizer() {
    if (!workspaceSplit || !panePdfWrapper || !splitResizer) return;

    const saved = localStorage.getItem('workspace_split_pct');
    if (saved) {
        panePdfWrapper.style.flexBasis = saved;
        workspaceSplit.style.setProperty('--split-pdf-pct', saved);
    }

    let dragging = false;

    const onMove = (clientX) => {
        const rect = workspaceSplit.getBoundingClientRect();
        const pct = ((clientX - rect.left) / rect.width) * 100;
        const clamped = Math.min(72, Math.max(22, pct));
        const basis = `${clamped}%`;
        panePdfWrapper.style.flexBasis = basis;
        workspaceSplit.style.setProperty('--split-pdf-pct', basis);
    };

    const stopDrag = () => {
        if (!dragging) return;
        dragging = false;
        splitResizer.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        localStorage.setItem('workspace_split_pct', panePdfWrapper.style.flexBasis);
    };

    splitResizer.addEventListener('mousedown', (e) => {
        if (workspaceSplit.classList.contains('pdf-hidden')) return;
        dragging = true;
        splitResizer.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        onMove(e.clientX);
    });

    document.addEventListener('mouseup', stopDrag);

    splitResizer.addEventListener('keydown', (e) => {
        if (workspaceSplit.classList.contains('pdf-hidden')) return;
        const rect = workspaceSplit.getBoundingClientRect();
        const current = (panePdfWrapper.getBoundingClientRect().width / rect.width) * 100;
        if (e.key === 'ArrowLeft') { onMove(rect.left + rect.width * (current - 2) / 100); e.preventDefault(); }
        if (e.key === 'ArrowRight') { onMove(rect.left + rect.width * (current + 2) / 100); e.preventDefault(); }
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            localStorage.setItem('workspace_split_pct', panePdfWrapper.style.flexBasis);
        }
    });
}

function setupPdfPaneToggle() {
    if (!workspaceSplit) return;

    const applyHidden = (hidden) => {
        workspaceSplit.classList.toggle('pdf-hidden', hidden);
        if (btnTogglePdf) {
            btnTogglePdf.textContent = hidden ? 'Hiện ảnh' : 'Ẩn ảnh';
            btnTogglePdf.title = hidden ? 'Hiện lại ảnh gốc' : 'Ẩn ảnh gốc để mở rộng bảng';
        }
        if (btnShowPdf) btnShowPdf.classList.toggle('hidden', !hidden);
        localStorage.setItem('workspace_pdf_hidden', hidden ? '1' : '0');
        if (!hidden && currentStep === 2 && isPageReady(currentPageNumber)) {
            requestAnimationFrame(() => renderBoundingBoxes());
        }
    };

    const saved = localStorage.getItem('workspace_pdf_hidden');
    if (saved === '1') applyHidden(true);

    const toggle = () => applyHidden(!workspaceSplit.classList.contains('pdf-hidden'));

    btnTogglePdf?.addEventListener('click', toggle);
    btnShowPdf?.addEventListener('click', toggle);
}

// ── Bounding boxes ──
function renderBoundingBoxes() {
    bboxContainer.innerHTML = '';
    const page = getPageData(currentPageNumber);
    if (!page) return;

    const scaleX = pdfPageImage.clientWidth / imageNaturalWidth;
    const scaleY = pdfPageImage.clientHeight / imageNaturalHeight;

    page.tables.forEach((table, tIdx) => {
        table.cells.forEach(cell => {
            if (!cell.bbox || cell.bbox.length !== 4) return;
            const box = document.createElement('div');
            box.className = 'bbox-highlight';
            box.id = `bbox-${tIdx}-${cell.row}-${cell.col}`;
            box.style.left = `${cell.bbox[0] * scaleX}px`;
            box.style.top = `${cell.bbox[1] * scaleY}px`;
            box.style.width = `${(cell.bbox[2] - cell.bbox[0]) * scaleX}px`;
            box.style.height = `${(cell.bbox[3] - cell.bbox[1]) * scaleY}px`;
            bboxContainer.appendChild(box);
        });
    });
}

window.addEventListener('resize', () => {
    if (currentStep === 2 && isPageReady(currentPageNumber)) renderBoundingBoxes();
});

// ── Table grid ──
function renderTableGrid() {
    tableScrollerZone.innerHTML = '';
    const page = getPageData(currentPageNumber);
    if (!page || !page.tables.length) {
        tableScrollerZone.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted)">Không phát hiện bảng trên trang này.</div>';
        updateValidationAlerts([]);
        return;
    }

    const allErrors = [];

    page.tables.forEach((table, tIdx) => {
        const section = document.createElement('div');
        section.style.marginBottom = '16px';

        const title = document.createElement('h4');
        title.style.cssText = 'font-size:13px;color:var(--red-primary);margin-bottom:8px';
        title.textContent = `Bảng ${tIdx + 1} (${table.num_rows} dòng × ${table.num_cols} cột)`;
        section.appendChild(title);

        const wrap = document.createElement('div');
        wrap.style.overflowX = 'auto';

        const grid = document.createElement('table');
        grid.className = 'ocr-table';

        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        for (let c = 1; c <= table.num_cols; c++) {
            headerRow.innerHTML += `<th>Cột ${c}</th>`;
        }
        thead.appendChild(headerRow);
        grid.appendChild(thead);

        const tbody = document.createElement('tbody');
        const cellMap = {};
        table.cells.forEach(c => { cellMap[`${c.row}_${c.col}`] = c; });

        for (let r = 0; r < table.num_rows; r++) {
            const tr = document.createElement('tr');
            for (let c = 0; c < table.num_cols; c++) {
                const cell = cellMap[`${r}_${c}`] || { text: '', confidence: 1.0 };
                const td = document.createElement('td');
                td.id = `cell-${tIdx}-${r}-${c}`;

                const err = validateCellText(r, c, cell.text, cell.confidence);
                if (err) {
                    allErrors.push({ tIdx, r, c, msg: err });
                    td.className = (err.includes('dấu') || cell.confidence < 0.85) ? 'cell-warn' : 'cell-err';
                } else if (cell.confidence < 0.85) {
                    td.className = 'cell-warn';
                }

                const cropStyle = getCropStyle(cell.bbox);
                td.innerHTML = `
                    <div class="cell-container">
                        <input type="text" class="cell-input" value="${escapeAttr(cell.text)}"
                            data-table="${tIdx}" data-row="${r}" data-col="${c}">
                        <div class="cell-zoom-tooltip">
                            <div class="tooltip-title">Ảnh gốc đối chiếu:</div>
                            <div class="tooltip-image-crop">
                                <div class="tooltip-image-crop-view" style="${cropStyle}"></div>
                            </div>
                            <div class="tooltip-meta">
                                <span>Tin cậy: ${(cell.confidence * 100).toFixed(0)}%</span>
                                ${err ? `<span class="tooltip-err-msg">${escapeAttr(err)}</span>` : ''}
                            </div>
                        </div>
                    </div>`;

                const input = td.querySelector('.cell-input');
                input.addEventListener('focus', () => focusCell(tIdx, r, c));
                input.addEventListener('blur', () => td.classList.remove('cell-focus'));
                input.addEventListener('change', (e) => handleCellChange(tIdx, r, c, e.target.value));

                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        grid.appendChild(tbody);
        wrap.appendChild(grid);
        section.appendChild(wrap);
        tableScrollerZone.appendChild(section);
    });

    updateValidationAlerts(allErrors);
}

function escapeAttr(str) {
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function getCropStyle(bbox) {
    if (!bbox || bbox.length < 4) return 'display:none';
    const [x1, y1, x2, y2] = bbox;
    const zoom = 1.3;
    const url = `${API_BASE}/api/ocr/result/${jobId}/page/${currentPageNumber}/image`;
    return `background-image:url(${url});background-position:-${x1 * zoom}px -${y1 * zoom}px;background-size:${imageNaturalWidth * zoom}px ${imageNaturalHeight * zoom}px;width:${(x2 - x1) * zoom}px;height:${(y2 - y1) * zoom}px`;
}

function focusCell(tIdx, r, c) {
    activeCell = { tableIdx: tIdx, row: r, col: c };
    document.querySelectorAll('.ocr-table td').forEach(td => td.classList.remove('cell-focus'));
    const el = document.getElementById(`cell-${tIdx}-${r}-${c}`);
    if (el) el.classList.add('cell-focus');

    document.querySelectorAll('.bbox-highlight').forEach(b => b.classList.remove('active'));
    const box = document.getElementById(`bbox-${tIdx}-${r}-${c}`);
    if (box) { box.classList.add('active'); box.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
}

function validateCellText(row, col, text, confidence) {
    if (row === 0) return '';
    if (col === 1 && text) {
        if (/\d/.test(text)) return 'Tên chứa chữ số';
        const raw = text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
        if (raw === text && text.trim().length > 3) return 'Họ tên thiếu dấu VN';
    }
    if (col === 2 && text) {
        if (!/^\d{12}$/.test(text.replace(/\s+/g, ''))) return 'CCCD phải có 12 số';
    }
    if (!text) return 'Không được trống';
    return '';
}

function updateValidationAlerts(errors) {
    if (errors.length) {
        alertsSummary.classList.remove('hidden');
        alertSummaryText.innerHTML = `Phát hiện <strong>${errors.length}</strong> cảnh báo / lỗi định dạng cần kiểm tra.`;
    } else {
        alertsSummary.classList.add('hidden');
    }
}

async function handleCellChange(tIdx, r, c, val) {
    const page = getPageData(currentPageNumber);
    if (!page) return;
    const cell = page.tables[tIdx]?.cells.find(cl => cl.row === r && cl.col === c);
    if (cell) { cell.text = val; cell.confidence = 1.0; }

    try {
        await fetch(`${API_BASE}/api/ocr/result/${jobId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates: [{ page_number: currentPageNumber, table_index: tIdx, row: r, col: c, text: val }] }),
        });
    } catch (e) { console.warn('Sync failed:', e); }

    renderTableGrid();
    setTimeout(() => {
        const input = document.querySelector(`.cell-input[data-table="${tIdx}"][data-row="${r}"][data-col="${c}"]`);
        if (input) { input.focus(); const v = input.value; input.value = ''; input.value = v; }
    }, 10);
}

// ── Keyboard nav ──
function setupKeyboard() {
    document.addEventListener('keydown', (e) => {
        if (currentStep !== 2 || !activeCell) return;
        const input = document.querySelector(
            `.cell-input[data-table="${activeCell.tableIdx}"][data-row="${activeCell.row}"][data-col="${activeCell.col}"]`
        );
        if (!input) return;

        const table = getPageData(currentPageNumber)?.tables[activeCell.tableIdx];
        if (!table) return;

        let tr = activeCell.row, tc = activeCell.col, moved = false;

        if (e.key === 'ArrowDown') { tr = Math.min(table.num_rows - 1, tr + 1); moved = true; }
        else if (e.key === 'ArrowUp') { tr = Math.max(0, tr - 1); moved = true; }
        else if (e.key === 'ArrowRight' && input.selectionStart === input.value.length) { tc = Math.min(table.num_cols - 1, tc + 1); moved = true; }
        else if (e.key === 'ArrowLeft' && input.selectionStart === 0) { tc = Math.max(0, tc - 1); moved = true; }

        if (moved) {
            e.preventDefault();
            document.querySelector(`.cell-input[data-table="${activeCell.tableIdx}"][data-row="${tr}"][data-col="${tc}"]`)?.focus();
        }
    });
}

// ── Submit ──
function submitBatch() {
    let total = 0;
    ocrData?.pages.forEach(p => p.tables.forEach(t => { total += Math.max(0, t.num_rows - 1); }));
    successBatchCode.textContent = `BATCH-${jobId.toUpperCase()}`;
    successTotalRecords.textContent = `${total} nhân sự`;
    stopPolling();
    setStep(3);
}
