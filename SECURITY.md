# Security Policy

The DABOOK team takes the security and integrity of our codebase, pipeline artifacts, and user environments very seriously. This document outlines our vulnerability reporting policy, supported versions, and best practices when processing untrusted inputs.

---

## Supported Versions

Only the latest active minor release receives active security patches.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1.0 | :x:                |

---

## Reporting a Vulnerability

If you believe you have discovered a security vulnerability in DABOOK, **please do NOT disclose it publicly** in a GitHub issue, public pull request, or discussion forum.

### Preferred Method: GitHub Private Vulnerability Reporting
Please use GitHub's built-in **[Private Vulnerability Reporting](https://github.com/brovk2008/Dabook/security/advisories/new)** feature:
1. Navigate to the DABOOK repository on GitHub.
2. Click on the **Security** tab.
3. Click **Report a vulnerability** under "Advisories".
4. Provide a detailed summary, proof of concept (PoC), and potential impact.

### Alternative Method: Direct Contact
If you are unable to use GitHub Security Advisories, please email:
- **Email:** `94829613+brovk2008@users.noreply.github.com`
- **Subject line:** `[SECURITY] Potential vulnerability in DABOOK`

### What to Include in Your Report
To help us triage and remediate the issue as quickly as possible, please include:
- A descriptive title and type of issue (e.g. arbitrary file write, resource exhaustion / DoS, unsafe deserialization).
- Steps to reproduce the issue (including sample code or a minimal reproducing file).
- The version of DABOOK, Python version, and operating system.
- Any potential remediations or patches you have identified.

### Response Timeline and SLAs
- **Initial Acknowledgement:** Within **48 hours** of receiving your report.
- **Triage & Assessment:** Within **7 days** with an initial severity rating (CVSS score).
- **Fix & Coordinated Disclosure:** We aim to release a patch within **30 days** of confirming the vulnerability. We will coordinate the disclosure date with you to ensure users have time to update.
- **Credit:** We will gladly credit you in our release notes and GitHub Advisory (unless you prefer to remain anonymous).

---

## Threat Model & Security Considerations

DABOOK processes external and potentially untrusted PDF documents. When designing and running DABOOK, keep the following security principles in mind:

### 1. Untrusted Document Parsing
- PDFs may contain malicious payloads, malformed cross-reference tables, deeply nested structures, or decompress bombs designed to trigger high memory allocation or CPU consumption.
- DABOOK enforces resource guards (`--min-free-gb`, `ram_ceiling_pct`, `vram_ceiling_pct`) and worker isolation to mitigate Out-Of-Memory (OOM) crashes.
- By default, DABOOK uses permissive, memory-safe parsers (`pypdfium2`, `pdfminer.six`).
- Do not run DABOOK worker processes with elevated/root privileges.

### 2. Local Control Room (Dashboard)
- By default, the localhost dashboard binds strictly to `127.0.0.1` (`localhost`).
- It is **not** exposed to public interfaces (`0.0.0.0`) by default.
- If running on a remote server, access the dashboard over an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 user@remote`) rather than opening port 8765 to the internet.

### 3. LLM API Keys & Telemetry
- Never commit `.env` files or API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) to version control.
- DABOOK reads keys strictly from environment variables and does not write credentials into workspace SQLite state databases or event logs.
