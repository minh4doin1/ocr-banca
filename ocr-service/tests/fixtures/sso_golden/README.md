# Fixtures for SSO OCR golden benchmarking.
#
# Layout expected:
#   tests/fixtures/sso_golden/
#     sample01.pdf          # scan/PDF nguồn
#     sample01.golden.xlsx  # Excel đáp án (cùng format export SSO 10 cột)
#
# Chạy:
#   python scripts/benchmark_sso_columns.py tests/fixtures/sso_golden/sample01.pdf \
#       --golden tests/fixtures/sso_golden/sample01.golden.xlsx --json out.json
#
# File PDF mẫu không được commit (kích thước lớn / dữ liệu PII) —
# thêm cục bộ khi đo accuracy.
