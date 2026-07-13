# browser-harness CLI

Installed via `uv tool install browser-harness`
Command: `browser-harness`

## Common Usage

```bash
browser-harness <<'PY'
new_tab("https://example.com")
print(page_info())
PY
```

```bash
browser-harness --doctor  # diagnose connection issues
browser-harness skill      # print skill markdown
browser-harness auth login # authenticate for cloud browsers
```
