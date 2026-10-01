# Support Guide

Thank you for using DABOOK! Here is how to get help, find answers to questions, or report issues.

---

## 1. Documentation & Doctor Command

Before reaching out, check whether the built-in diagnostic tools can help:

- **DABOOK Doctor:**
  Run the diagnostics command in your terminal:
  ```bash
  dabook doctor
  ```
  This checks your Python environment, database health, disk space, and installed extraction backends.

- **Check Logs:**
  Supervisor and worker logs are stored inside your workspace:
  ```bash
  ./dabook_workspace/logs/
  ```

---

## 2. Asking Questions & Discussions

If you have questions about how to use DABOOK, how to integrate custom models, or general questions about Book Graph extraction:

- Check existing [GitHub Issues](https://github.com/brovk2008/Dabook/issues) to see if someone has asked the same question.
- Open an issue labeled with `question` on GitHub.

---

## 3. Reporting Bugs

If you have found a bug in DABOOK:
1. Ensure the bug is reproducible with the latest release or on `main`.
2. Check if a similar issue has already been reported.
3. [Open a Bug Report](https://github.com/brovk2008/Dabook/issues/new?template=bug_report.yml) with:
   - Your Python and OS version.
   - Output from `dabook doctor`.
   - Minimal reproduction steps.
   - Any stack traces from `./dabook_workspace/logs/`.

---

## 4. Feature Requests

Have an idea for a new dataset export format, extraction backend, or control room feature?
- [Submit a Feature Request](https://github.com/brovk2008/Dabook/issues/new?template=feature_request.yml) explaining your use case and proposed solution.

---

## 5. Security Vulnerabilities

Please **do not** use public issues for reporting security vulnerabilities. Refer to our [Security Policy](SECURITY.md) for instructions on confidential disclosure.
