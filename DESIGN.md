# Design: SSRF Guard for URL-Based File Sources

## Summary

Add controls to the file upload endpoint's URL-fetching capability so that ravnar cannot be used as a proxy to reach internal infrastructure or cloud metadata endpoints. URL fetching will be disabled by default, and when enabled, restricted to an explicit allowlist of permitted domains. The implementation adds a new configuration sub-object to the storage config and a validation layer in `FileHandler`.

## Goals

- Prevent ravnar from being coerced into making HTTP requests to internal or private IP ranges.
- Prevent data exfiltration via attacker-controlled URLs.
- Prevent DNS-rebinding-based bypasses through defense-in-depth design (per-hop redirect validation, hostname normalization).
- Maintain existing functionality for legitimate use cases (fetching files from known external domains).

## Non-Goals

- Adding a full HTTP proxy or egress filtering system — that is the deployment environment's responsibility.
- Rate-limiting or connection-pool sizing — these are gateway concerns (per established architecture boundaries).
- Adding support for authenticated URL fetches (e.g., Bearer tokens, cookies) — the URL source is intended for public resources only.
- Replacing the underlying HTTP client library or adding a custom DNS resolver.
- DNS-level rebinding protection (see Tradeoffs & Risks).
- Backwards compatibility for config structure — ravnar is in alpha.

## Background / Motivation

Services that fetch content from user-supplied URLs are a well-known vector for Server-Side Request Forgery (SSRF). An attacker who can supply a URL to the server can probe internal services, reach cloud metadata endpoints (e.g., `169.254.169.254`), or exfiltrate data to an attacker-controlled endpoint via query parameters, path segments, or DNS lookups.

The current implementation in `FileHandler._extract_url` creates an `httpx.AsyncClient` with `follow_redirects=True` and no transport-level restrictions. Any authenticated user with `files:write` can supply any URL. This means:

- An attacker can reach any internal service on the host or network that ravnar can reach.
- An attacker can use redirect chains (allowed → internal) to bypass naive hostname filtering.
- An attacker can use IPv6 literal addresses, DNS rebinding, or alternative representations of internal IPs.

The fix follows a deny-by-default model: URL fetching is opt-in, and when enabled, only explicitly listed domains are permitted.

## Design

### 1. Configuration Model

Restructure the `StorageConfig` hierarchy to split the monolithic config into sub-objects. Add a new `URLDataSourceConfig` for the SSRF guard settings.

#### Current structure (before)

```python
class StorageConfig(BaseModel):
    enabled: bool = True
    database_dsn: str = ...
    file_storage_path: UPath = ...
```

#### New structure (after)

```python
class DatabaseConfig(BaseModel):
    dsn: str = ...

class URLDataSourceConfig(BaseModel):
    enabled: bool = False
    allowlist: list[str] = []
    timeout_seconds: int = 30

class FileStorageConfig(BaseModel):
    path: UPath = ...
    url_data_source: URLDataSourceConfig = Field(default_factory=URLDataSourceConfig)

class StorageConfig(BaseModel):
    enabled: bool = True
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    files: FileStorageConfig = Field(default_factory=FileStorageConfig)
```

- `storage.enabled` remains a top-level toggle that disables all stateful routes (database, files, threads).
- `Database` takes a `DatabaseConfig` instead of a raw DSN string.
- `FileHandler` takes a `FileStorageConfig` instead of raw `root` and `database` (plus other relevant params).

##### `url_data_source` properties

| Property | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | When false, any file source with `type: url` returns a 400 error. |
| `allowlist` | `list[str]` | `[]` | Case-insensitive domain list. Only URLs whose hostname matches an entry (or is a subdomain of an entry) are permitted. When empty and `enabled` is true, all URLs are rejected. Each entry is normalized via the IDNA punycode encoder before storage. |
| `timeout_seconds` | `int` | `30` | Per-request timeout for DNS + connect + read of the URL fetch. |

##### Example YAML

```yaml
storage:
  enabled: true
  database:
    dsn: sqlite:///data/state.db
  files:
    path: /data/files
    url_data_source:
      enabled: true
      allowlist:
        - "raw.githubusercontent.com"
        - "github.com"
      timeout_seconds: 30
```

### 2. IDNA / Punycode Normalization Helper

Write a small shared helper function to normalize hostnames before comparison. It is called both:

- At config load time, to normalize each entry in `url_fetch_allowlist`.
- At request time, to normalize the extracted hostname from the user-supplied URL.

```python
def normalize_hostname(host: str) -> str:
    """Normalize a hostname to lowercase ASCII (punycode form).

    Handles internationalized domain names by encoding them to
    their IDNA2003 ASCII-compatible form. Pure-ASCII inputs are
    lowercased and returned as-is.

    Raises ValueError if the hostname is not valid IDNA.
    """
    return host.encode("idna").decode("ascii").lower()
```

Using `str.encode("idna")` and then decoding back to ASCII ensures that:

- `"München.example.com"` → `"xn--mnchen-3ya.example.com"`
- `"GITHUB.COM"` → `"github.com"`
- `"xn--mnchen-3ya.example.com"` → `"xn--mnchen-3ya.example.com"` (idempotent)

### 3. Validation Logic in FileHandler

Add a validation method `_validate_url` in `FileHandler`, called at the start of `_extract_url` and after each redirect hop.

```python
async def _validate_url(self, url: str) -> str:
    """Validate a URL against the SSRF guard config.

    Returns the validated URL string on success.
    Raises HTTPException(400) on failure.
    """
    config = self._file_storage_config.url_data_source

    if not config.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL file source is not enabled")

    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

    normalized = normalize_hostname(hostname)

    # Allowlist check
    if not config.allowlist:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

    allowed = False
    for entry in config.allowlist:
        entry_norm = normalize_hostname(entry)
        if normalized == entry_norm or normalized.endswith("." + entry_norm):
            allowed = True
            break

    if not allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

    return url
```

Key points:

- **IP-literal hostnames** (IPv4 dotted, IPv6 colon-hex, bracketed IPv6) are rejected unless the operator explicitly adds the IP string to the allowlist. This is a natural consequence of the allowlist model — `"93.184.216.34"` would match `normalize_hostname("93.184.216.34")` → `"93.184.216.34"`.
- **Error messages are generic** — never include the blocked hostname, the allowlist entry checked, or any internal details. All diagnostic information goes to the OpenTelemetry trace.
- **Port numbers** are handled naturally by `urllib.parse.urlparse` — `urlparse` separates hostname from port, so `github.com:8080` extracts hostname `"github.com"`.
- **Trailing dots** in hostnames (valid DNS root references like `github.com.`) are **not** normalized by this code. If `github.com.` reaches the validator, its hostname is `"github.com."` which won't match `"github.com"`. This is acceptable — operators should not use trailing dots. No normalization is attempted.

### 4. Redirect Handling

Set `follow_redirects=False` on the `httpx.AsyncClient` and implement a manual redirect loop within `_extract_url`:

1. Issue the initial GET request with `follow_redirects=False`.
2. If the response is a redirect (3xx with a `Location` header), extract the redirect target URL.
3. Validate the target URL through `_validate_url`.
4. Issue a new GET request to the validated target.
5. Repeat up to a maximum of 20 redirects (httpx default).
6. On success, proceed with content extraction as before.

This approach ensures **every hop** in a redirect chain is validated against the allowlist. An allowlisted domain cannot redirect to an internal IP without being caught.

**No config option to disable redirects.** Redirects are always allowed, subject to per-hop allowlist validation. A redirects-enabled toggle would create an operator footgun without meaningful security benefit.

### 5. Error Responses

All blocked URL fetch requests return a `400 Bad Request` with a generic `detail` string, using FastAPI's standard `HTTPException`:

```python
raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL file source is not enabled")
# or
raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")
```

These match the existing pattern used by `_extract_custom` for unsupported source types (`422` for invalid source type, `400` for blocked URL).

### 6. Tracing

The existing `"FileHandler.fetch_url"` OpenTelemetry span is extended with attributes for diagnostics:

- `ssrf.blocked_reason` — `"not_enabled"` or `"not_allowed"` (when the request is rejected)
- `ssrf.hostname` — the normalized hostname that was checked
- `ssrf.allowlist_entry` — the allowlist entry that matched (if applicable)
- `ssrf.redirect_chain` — list of URLs visited in the redirect chain
- `ssrf.redirect_count` — number of redirect hops followed

No dedicated structured log lines are emitted beyond tracing. The trace provides full detail for debugging; logs do not need to duplicate it.

## Tradeoffs & Risks

- **Usability vs. security:** Disabling URL fetching by default breaks any workflow that depends on it until the operator explicitly configures it. This is intentional — SSRF is a critical-class vulnerability and should require deliberate enabling. The error message directs operators to the configuration option indirectly (generic "not enabled" message).
- **Allowlist granularity:** Domain-level allowlisting is coarse. An attacker who controls a subdomain of an allowlisted domain (e.g., `evil.github.io` if `github.io` is allowlisted) could still abuse it. More granular approaches (path-based, content-type-based) add complexity. The allowlist is documented as a security boundary that operators must configure carefully.
- **DNS rebinding:** An attacker who controls a domain and its authoritative DNS server can return different IPs for successive queries from the same client. If the first query (during allowlist validation) returns a public IP and the second query (during the actual HTTP request) returns an internal IP, the request reaches an internal target despite the allowlist check. Full protection requires a custom transport layer that pins DNS resolution — out of scope for this design. The per-hop redirect validation and IP-literal rejection mitigate simpler bypass variants. This is an accepted risk for the initial implementation.
- **IDNA2003 vs IDNA2008:** Python's `encode("idna")` implements IDNA2003. Some Unicode characters handled by IDNA2008 (e.g., `ß` → `"ss"`) may produce unexpected results. This is acceptable for an alpha-stage project. If edge cases arise, the normalization helper can be swapped for an IDNA2008 library.
- **Performance:** URL validation is cheap (string comparison). The HTTP timeout prevents resource exhaustion from a slow peer.
- **No logging beyond tracing:** Detailed diagnostic data is stored in OpenTelemetry spans, not in structured logs. This keeps log volume low for normal operation. Debugging a blocked request requires accessing trace data.

## Testing Strategy

- **Unit tests for `normalize_hostname`:** ASCII lowercasing, Unicode → punycode, already-punycode idempotency, invalid IDNA raises `ValueError`.
- **Unit tests for `_validate_url`:**
  - `url_fetch_enabled = false` → 400.
  - `url_fetch_allowlist = []` with enabled → 400.
  - Exact match, subdomain match, case-insensitive match.
  - Non-match → 400.
  - IP literal hostname (not in allowlist) → 400.
  - IP literal hostname (in allowlist as string) → allowed.
  - IDN hostname matching IDN allowlist entry.
  - IDN hostname matching punycode allowlist entry.
  - Hostname with trailing dot (not matching) → 400.
  - URL with userinfo (`user:pass@host`).
  - URL with non-standard port.
- **Unit tests for redirect loop:**
  - Single redirect to allowlisted domain → success.
  - Single redirect to non-allowlisted domain → 400.
  - Chain of consecutive redirects staying within allowlist → success.
  - Chain that eventually leaves allowlist → 400.
  - Exceeding max redirect count → error.
- **Integration tests:**
  - Start ravnar with URL fetching enabled and a known allowlist.
  - Upload file from allowlisted URL → success.
  - Upload file from non-allowlisted URL → 400.
  - Upload file with `type: url` when fetching is disabled → 400.
- **No e2e tests needed** beyond the integration coverage.

## Open Questions

*(none — all design decisions are resolved)*
