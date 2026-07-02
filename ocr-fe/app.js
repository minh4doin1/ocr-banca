/* ============================================================
   Banca OCR System — Standalone Client Logic (Offline Friendly)
   ============================================================ */

const API_BASE = window.location.port === '8100' ? '' : 'http://localhost:8100';

// App State
let currentStep = 0;
let selectedFile = null;
let jobId = '';
let totalPages = 0;
let currentPageNumber = 1;
let ocrData = null;
let activeCell = null; // { tableIdx, row, col }
let imageNaturalWidth = 1;
let imageNaturalHeight = 1;

// Elements
const fileInput = document.getElementById('file-input');
const selectBtn = document.getElementById('select-btn');
const dropZone = document.getElementById('drop-zone');
const modeSelector = document.getElementById('mode-selector');
const providerSelector = document.getElementById('provider-selector');
const apiProviderSelect = document.getElementById('api-provider');

const viewUpload = document.getElementById('view-upload');
const viewProcessing = document.getElementById('view-processing');
const viewWorkspace = document.getElementById('view-workspace');
const viewSuccess = document.getElementById('view-success');

const progressBarFill = document.getElementById('progress-bar-fill');
const progressStatusText = document.getElementById('progress-status-text');

const pdfPaneTitle = document.getElementById('pdf-pane-title');
const btnPrevPage = document.getElementById('btn-prev-page');
const btnNextPage = document.getElementById('btn-next-page');
const pdfPageImage = document.getElementById('pdf-page-image');
const bboxContainer = document.getElementById('bbox-container');
const pdfImageWrapper = document.getElementById('pdf-image-wrapper');

const btnExportExcel = document.getElementById('btn-export-excel');
const btnRestart = document.getElementById('btn-restart');
const btnConfirmBatch = document.getElementById('btn-confirm-batch');
const alertsSummary = document.getElementById('alerts-summary');
const alertSummaryText = document.getElementById('alert-summary-text');
const tableScrollerZone = document.getElementById('table-scroller-zone');

const successBatchCode = document.getElementById('success-batch-code');
const successTotalRecords = document.getElementById('success-total-records');
const btnSuccessNew = document.getElementById('btn-success-new');
const btnSuccessHome = document.getElementById('btn-success-home');

// Indicators
const indicators = [
    document.getElementById('step-0-indicator'),
    document.getElementById('step-1-indicator'),
    document.getElementById('step-2-indicator'),
    document.getElementById('step-3-indicator')
];

// --- INITIALIZATION ---
window.addEventListener('DOMContentLoaded', () => {
    setupUploadHandlers();
    setupWorkspaceHandlers();
    setupKeyboardNavigation();
});

// --- STEP NAVIGATION ---
function setStep(step) {
    currentStep = step;
    
    // Update Stepper indicators
    indicators.forEach((indicator, index) => {
        indicator.classList.remove('active', 'completed');
        if (index === step) {
            indicator.classList.add('active');
        } else if (index < step) {
            indicator.classList.add('completed');
        }
    });

    // Toggle View Panels
    viewUpload.classList.add('hidden');
    viewProcessing.classList.add('hidden');
    viewWorkspace.classList.add('hidden');
    viewSuccess.classList.add('hidden');

    if (step === 0) viewUpload.classList.remove('hidden');
    else if (step === 1) viewProcessing.classList.remove('hidden');
    else if (step === 2) viewWorkspace.classList.remove('hidden');
    else if (step === 3) viewSuccess.classList.remove('hidden');
}

// --- UPLOAD HANDLERS ---
function setupUploadHandlers() {
    selectBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) handleFile(e.target.files[0]);
    });

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--primary-color)';
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.style.borderColor = 'var(--border-color)';
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--border-color)';
        if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
    });

    document.querySelectorAll('input[name="processing-mode"]').forEach((radio) => {
        radio.addEventListener('change', syncModeUi);
    });
    syncModeUi();
}

function handleFile(file) {
    if (file.type !== 'application/pdf') {
        alert('Chỉ hỗ trợ tải lên file dạng PDF.');
        return;
    }
    selectedFile = file;
    setStep(1);
    uploadPdfToServer(file);
}

// --- API ACTIONS ---
async function uploadPdfToServer(file) {
    updateProgress(5, "Đang upload file lên server...");
    
    try {
        const processingMode = getSelectedProcessingMode();
        const formData = new FormData();
        formData.append('file', file);
        formData.append('processing_mode', processingMode);
        if (processingMode === 'api') {
            formData.append('api_provider', apiProviderSelect.value || 'ocrspace');
        }

        const res = await fetch(`${API_BASE}/api/ocr/upload`, {
            method: 'POST',
            body: formData
        });

        if (!res.ok) throw new Error("Upload failed");

        const data = await res.json();
        jobId = data.job_id;
        if (data.processing_mode) {
            progressStatusText.innerText = `Đang chạy OCR (${String(data.processing_mode).toUpperCase()})...`;
        }
        pollStatus();
    } catch (err) {
        alert("Lỗi tải file. Vui lòng đảm bảo ocr-service backend đang chạy ở cổng 8100.");
        setStep(0);
    }
}

function getSelectedProcessingMode() {
    const checked = document.querySelector('input[name="processing-mode"]:checked');
    return checked ? checked.value : 'local';
}

function syncModeUi() {
    const mode = getSelectedProcessingMode();
    if (mode === 'api') {
        providerSelector.classList.remove('hidden');
    } else {
        providerSelector.classList.add('hidden');
    }
}

function updateProgress(percent, text) {
    progressBarFill.style.width = `${percent}%`;
    progressStatusText.innerText = text;
}

function pollStatus() {
    const interval = setInterval(async () => {
        try {
            const res = await fetch(`${API_BASE}/api/ocr/status/${jobId}`);
            if (!res.ok) throw new Error("Status check failed");

            const job = await res.json();
            totalPages = job.total_pages;

            if (job.status === 'completed') {
                clearInterval(interval);
                updateProgress(100, "Xử lý thành công!");
                fetchOcrResult();
            } else if (job.status === 'failed') {
                clearInterval(interval);
                alert(`Lỗi xử lý OCR từ backend: ${job.error_message}`);
                setStep(0);
            } else {
                const percent = job.total_pages > 0
                    ? Math.min(95, Math.round((job.progress / job.total_pages) * 100))
                    : 40;
                updateProgress(percent, `Đang chạy phân tích OCR trang ${job.progress}/${job.total_pages || '...'}`);
            }
        } catch (e) {
            clearInterval(interval);
            alert("Lỗi kết nối kiểm tra tiến trình OCR.");
            setStep(0);
        }
    }, 1500);
}

async function fetchOcrResult() {
    try {
        const res = await fetch(`${API_BASE}/api/ocr/result/${jobId}`);
        if (!res.ok) throw new Error("Failed to load result");
        
        ocrData = await res.json();
        currentPageNumber = 1;
        
        renderWorkspace();
        setStep(2);
    } catch (e) {
        alert("Lỗi lấy dữ liệu OCR.");
        setStep(0);
    }
}

// --- WORKSPACE RENDERING ---
function renderWorkspace() {
    if (!ocrData) return;

    // Update Titles & Buttons
    pdfPaneTitle.innerText = `Tài liệu gốc (Trang ${currentPageNumber}/${totalPages})`;
    btnPrevPage.disabled = currentPageNumber <= 1;
    btnNextPage.disabled = currentPageNumber >= totalPages;

    // Load PDF Page Image
    const imgUrl = `${API_BASE}/api/ocr/result/${jobId}/page/${currentPageNumber}/image`;
    pdfPageImage.src = imgUrl;
    bboxContainer.innerHTML = ''; // clear overlays
    
    pdfPageImage.onload = () => {
        imageNaturalWidth = pdfPageImage.naturalWidth || 1;
        imageNaturalHeight = pdfPageImage.naturalHeight || 1;
        renderBoundingBoxes();
    };

    // Render Table Grid
    renderTableGrid();
}

function changePage(offset) {
    const nextPg = currentPageNumber + offset;
    if (nextPg >= 1 && nextPg <= totalPages) {
        currentPageNumber = nextPg;
        activeCell = null;
        renderWorkspace();
    }
}

function setupWorkspaceHandlers() {
    btnPrevPage.addEventListener('click', () => changePage(-1));
    btnNextPage.addEventListener('click', () => changePage(1));
    btnRestart.addEventListener('click', () => setStep(0));

    btnExportExcel.addEventListener('click', () => {
        window.open(`${API_BASE}/api/ocr/result/${jobId}/export`);
    });

    btnConfirmBatch.addEventListener('click', submitBatch);

    btnSuccessNew.addEventListener('click', () => setStep(0));
    btnSuccessHome.addEventListener('click', () => {
        window.location.href = '/';
    });
}

// Render bounding boxes on top of PDF image
function renderBoundingBoxes() {
    bboxContainer.innerHTML = '';
    const page = ocrData.pages.find(p => p.page_number === currentPageNumber);
    if (!page) return;

    const imgWidth = pdfPageImage.clientWidth;
    const imgHeight = pdfPageImage.clientHeight;
    const scaleX = imgWidth / imageNaturalWidth;
    const scaleY = imgHeight / imageNaturalHeight;

    page.tables.forEach((table, tIdx) => {
        table.cells.forEach(cell => {
            if (cell.bbox && cell.bbox.length === 4) {
                const box = document.createElement('div');
                box.className = 'bbox-highlight';
                box.id = `bbox-${tIdx}-${cell.row}-${cell.col}`;
                
                const left = cell.bbox[0] * scaleX;
                const top = cell.bbox[1] * scaleY;
                const w = (cell.bbox[2] - cell.bbox[0]) * scaleX;
                const h = (cell.bbox[3] - cell.bbox[1]) * scaleY;

                box.style.left = `${left}px`;
                box.style.top = `${top}px`;
                box.style.width = `${w}px`;
                box.style.height = `${h}px`;

                bboxContainer.appendChild(box);
            }
        });
    });
}

// Re-evaluate bbox overlay sizes when window resizing
window.addEventListener('resize', () => {
    if (currentStep === 2) {
        renderBoundingBoxes();
    }
});

// Render dynamic tables
function renderTableGrid() {
    tableScrollerZone.innerHTML = '';
    const page = ocrData.pages.find(p => p.page_number === currentPageNumber);
    if (!page || !page.tables.length) {
        tableScrollerZone.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Không phát hiện thấy bảng trên trang này.</div>';
        updateValidationAlerts([]);
        return;
    }

    const allErrors = [];

    page.tables.forEach((table, tIdx) => {
        const section = document.createElement('div');
        section.className = 'ocr-table-section';
        section.style.marginBottom = '20px';

        const title = document.createElement('div');
        title.innerHTML = `<h4 style="margin-bottom: 8px; color: var(--primary-color);">Bảng ${tIdx + 1} (${table.num_rows} dòng × ${table.num_cols} cột)</h4>`;
        section.appendChild(title);

        const scrollWrap = document.createElement('div');
        scrollWrap.style.overflowX = 'auto';

        const grid = document.createElement('table');
        grid.className = 'ocr-table';

        // Headers
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        for (let col = 1; col <= table.num_cols; col++) {
            headerRow.innerHTML += `<th>Cột ${col}</th>`;
        }
        thead.appendChild(headerRow);
        grid.appendChild(thead);

        // Grid contents
        const tbody = document.createElement('tbody');
        
        // Map cells to a fast 2D lookup
        const cellMap = {};
        table.cells.forEach(cell => {
            cellMap[`${cell.row}_${cell.col}`] = cell;
        });

        for (let r = 0; r < table.num_rows; r++) {
            const tr = document.createElement('tr');
            for (let c = 0; c < table.num_cols; c++) {
                const cell = cellMap[`${r}_${c}`] || { text: '', confidence: 1.0 };
                const td = document.createElement('td');
                td.id = `cell-${tIdx}-${r}-${c}`;
                
                // Real-time rules validation
                const errMessage = validateCellText(r, c, cell.text, cell.confidence);
                if (errMessage) {
                    allErrors.push({ table: tIdx, row: r, col: c, msg: errMessage });
                    
                    if (errMessage.includes('dấu') || cell.confidence < 0.85) {
                        td.className = 'cell-warn';
                    } else {
                        td.className = 'cell-err';
                    }
                } else if (cell.confidence < 0.85) {
                    td.className = 'cell-warn';
                }

                // Cell layout & Zoom tooltip crop styles
                const cropStyle = getCropBackgroundStyle(cell.bbox);

                td.innerHTML = `
                    <div class="cell-container">
                        <input type="text" class="cell-input" value="${cell.text}" 
                            data-table="${tIdx}" data-row="${r}" data-col="${c}">
                        <div class="cell-zoom-tooltip">
                            <div class="tooltip-title">Ảnh gốc đối chiếu:</div>
                            <div class="tooltip-image-crop">
                                <div class="tooltip-image-crop-view" style="${cropStyle}"></div>
                            </div>
                            <div class="tooltip-meta">
                                <span>Độ tin cậy: ${(cell.confidence * 100).toFixed(0)}%</span>
                                ${errMessage ? `<span class="tooltip-err-msg">${errMessage}</span>` : ''}
                            </div>
                        </div>
                    </div>
                `;

                // Handle focus and blur for highlighting
                const input = td.querySelector('.cell-input');
                input.addEventListener('focus', () => {
                    focusCell(tIdx, r, c);
                });
                input.addEventListener('blur', () => {
                    td.classList.remove('cell-focus');
                });
                input.addEventListener('change', (e) => {
                    handleCellChange(tIdx, r, c, e.target.value);
                });

                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        grid.appendChild(tbody);
        scrollWrap.appendChild(grid);
        section.appendChild(scrollWrap);
        tableScrollerZone.appendChild(section);
    });

    updateValidationAlerts(allErrors);
}

function getCropBackgroundStyle(bbox) {
    if (!bbox || bbox.length < 4) return 'display:none;';
    const x1 = bbox[0];
    const y1 = bbox[1];
    const w = bbox[2] - x1;
    const h = bbox[3] - y1;

    const zoom = 1.3;
    const imgUrl = `${API_BASE}/api/ocr/result/${jobId}/page/${currentPageNumber}/image`;

    return `
        background-image: url(${imgUrl});
        background-position: -${x1 * zoom}px -${y1 * zoom}px;
        background-size: ${imageNaturalWidth * zoom}px ${imageNaturalHeight * zoom}px;
        width: ${w * zoom}px;
        height: ${h * zoom}px;
    `;
}

// Focus actions: highlights matching cell and sync-scrolls the PDF page view to highlight
function focusCell(tIdx, r, c) {
    activeCell = { tableIdx: tIdx, row: r, col: c };

    // Clear previous cell highlight
    document.querySelectorAll('.ocr-table td').forEach(td => td.classList.remove('cell-focus'));
    
    // Highlight new cell
    const cellElement = document.getElementById(`cell-${tIdx}-${r}-${c}`);
    if (cellElement) cellElement.classList.add('cell-focus');

    // Highlight PDF Bounding box
    document.querySelectorAll('.bbox-highlight').forEach(box => box.classList.remove('active'));
    const boxElement = document.getElementById(`bbox-${tIdx}-${r}-${c}`);
    if (boxElement) {
        boxElement.classList.add('active');
        
        // Scroll left pane to show active box
        boxElement.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
    }
}

// Rule validations (CCCD 12 numbers, Accents on Vietnamese name)
function validateCellText(row, col, text, confidence) {
    if (row === 0) return ''; // Skip headers
    
    // Assume Col 1 is Name
    if (col === 1 && text) {
        if (/\d/.test(text)) return 'Tên chứa chữ số';
        
        // Viet accents check
        const rawAccentless = text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
        if (rawAccentless === text && text.trim().length > 3) {
            return 'Họ tên thiếu dấu VN';
        }
    }

    // Assume Col 2 is CCCD
    if (col === 2 && text) {
        const clean = text.replace(/\s+/g, '');
        if (!/^\d{12}$/.test(clean)) return 'CCCD phải có 12 số';
    }

    // Empty fields check
    if (!text) {
        return 'Không được trống';
    }

    return '';
}

function updateValidationAlerts(errors) {
    if (errors.length > 0) {
        alertsSummary.style.display = 'block';
        alertSummaryText.innerHTML = `Phát hiện <strong>${errors.length} cảnh báo / định dạng sai</strong> cần kiểm soát đối chiếu.`;
    } else {
        alertsSummary.style.display = 'none';
    }
}

async function handleCellChange(tIdx, r, c, newValue) {
    // 1. Update local ocrData memory
    const page = ocrData.pages.find(p => p.page_number === currentPageNumber);
    if (!page) return;
    const table = page.tables[tIdx];
    if (!table) return;
    const cell = table.cells.find(cell => cell.row === r && cell.col === c);
    if (cell) {
        cell.text = newValue;
        cell.confidence = 1.0; // user validated
    }

    // 2. Put update to backend API
    try {
        await fetch(`${API_BASE}/api/ocr/result/${jobId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                updates: [
                    {
                        page_number: currentPageNumber,
                        table_index: tIdx,
                        row: r,
                        col: c,
                        text: newValue
                    }
                ]
            })
        });
    } catch (e) {
        console.warn("Backend sync failed:", e);
    }

    // 3. Re-render table elements to refresh error highlight classes
    renderTableGrid();
    
    // Maintain active focus after re-rendering
    setTimeout(() => {
        const input = document.querySelector(`.cell-input[data-table="${tIdx}"][data-row="${r}"][data-col="${c}"]`);
        if (input) {
            input.focus();
            // Move cursor to end of string
            const val = input.value;
            input.value = '';
            input.value = val;
        }
    }, 10);
}

// Keyboard navigation rules (Excel-like arrows and tab controls)
function setupKeyboardNavigation() {
    document.addEventListener('keydown', (e) => {
        if (currentStep !== 2 || !activeCell) return;

        const currentInput = document.querySelector(
            `.cell-input[data-table="${activeCell.tableIdx}"][data-row="${activeCell.row}"][data-col="${activeCell.col}"]`
        );
        if (!currentInput) return;

        let targetRow = activeCell.row;
        let targetCol = activeCell.col;
        let change = false;

        const table = ocrData.pages.find(p => p.page_number === currentPageNumber).tables[activeCell.tableIdx];
        if (!table) return;

        if (e.key === 'ArrowDown') {
            targetRow = Math.min(table.num_rows - 1, activeCell.row + 1);
            change = true;
            e.preventDefault();
        } else if (e.key === 'ArrowUp') {
            targetRow = Math.max(0, activeCell.row - 1);
            change = true;
            e.preventDefault();
        } else if (e.key === 'ArrowRight' && currentInput.selectionStart === currentInput.value.length) {
            targetCol = Math.min(table.num_cols - 1, activeCell.col + 1);
            change = true;
            e.preventDefault();
        } else if (e.key === 'ArrowLeft' && currentInput.selectionStart === 0) {
            targetCol = Math.max(0, activeCell.col - 1);
            change = true;
            e.preventDefault();
        }

        if (change) {
            const nextInput = document.querySelector(
                `.cell-input[data-table="${activeCell.tableIdx}"][data-row="${targetRow}"][data-col="${targetCol}"]`
            );
            if (nextInput) {
                nextInput.focus();
            }
        }
    });
}

// --- CONFIRM BATCH CREATION ---
function submitBatch() {
    // Collect stats
    let totalRecords = 0;
    ocrData.pages.forEach(p => {
        p.tables.forEach(t => {
            totalRecords += Math.max(0, t.num_rows - 1);
        });
    });

    successBatchCode.innerText = `BATCH-${jobId.toUpperCase()}`;
    successTotalRecords.innerText = `${totalRecords || 12} nhân sự`;
    
    setStep(3);
}
