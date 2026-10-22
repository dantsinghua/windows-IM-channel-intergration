# its-relay production transport (2026-10-22)

Production origin: `https://its-relay.yingmi.cn`.
The client appends `/api/files/pending`, `/api/files/upload`, or `/api/files/download`.
Do not append `/api/files` to base_url. Each request sends a fresh `accesstoken` header; the old underscore header is not sent.

mTLS is required for this origin. Set `client_cert_file` and `client_key_file` to deployment-owned files on the machine running the Python service. `ca_file` is optional and is for verification of the SERVER certificate, not the client certificate. Omit it to use the OS trust store. TLS hostname and chain verification remain enabled. HTTP downgrade and redirects are rejected.

Paths in xunjia/njcb JSON configurations are relative to that configuration's directory. Paths entered in the collector UI must refer to files on the collector runtime (use absolute paths). Never paste a PEM private key into the credential YAML. Keep private keys readable only by the deployment user, and exclude all real keys/certificates from source control and release bundles.

xunjia and Windows IM use their existing institution/workbench/account path calculation. Replacing the origin or credentials starts a fresh business context under the already-approved reset semantics; no old commands are replayed. Configuring TLS alone does not prove reachability or native IM delivery.

The deposit project reads `NJCB_ITS_RELAY_CONFIG` (JSON) with `base_url`, `path`, `auth_file`, and the TLS file paths. It provides authenticated `/api/its-relay/status`, `/pending`, POST `/upload`, and POST `/download`, plus `njcb-relay --config <file> pending|upload|download`. The upstream download removes a queued object, so it is never triggered automatically by a status page, redirected, or retried after an unknown outcome. This low-level file transport does not silently replace the unrelated mail ingestion or OSS catalog business schema.

Validate pending first. For a file round-trip use a NEW isolated queue name (maximum 20 ASCII letters/digits/underscores/hyphens), record the file hash, upload once, download once, compare the complete bytes, and check empty. Do not use a business queue or the example lxz001 path for a test.

## Verification boundary for this change

The production credentialed network probe was blocked by the execution platform before execution. No production HTTP status has been observed. The loopback HTTPS tests use a generated CA, client certificate, real TLS sockets, strict mutual authentication, and byte-exact upload/download. They do not establish production availability.
