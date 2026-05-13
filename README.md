# i3xua

An i3X HTTPS Wrapper for OPC UA Servers.

i3xua translates between **servers** that use OPC UA TCP Binary, a rich and complex systems classification and binary protocol specification
commonly found in industrial settings, and **clients** that use i3X, an easily-implemented HTTP & JSON-based protocol specification for safely integrating 
industrial systems as web-oriented, ad hoc resources in a secure, standard way. The wrapper implements prescribed i3X and UA security layers, maintains live OPC UA sessions, mirrors UA server address spaces into i3X, and exposes 
browse / read / subscribe / history endpoints to i3X clients.

A walkthrough of the application is rendered at
[**docs/slides.pdf**](docs/slides.pdf)

## 1. Configure `config.yaml`

Minimal working config:

```yaml
server:
  host: 127.0.0.1
  port: 8080
  auth:
    mode: none                # None | Bearer | Basic
  user: 
    type: anonymous 
connections:
  - name: my-ua-server
    endpoint: opc.tcp://ipAddress:port
    channel:
      mode: none              # None | Sign | SignAndEncrypt
    namespace_allowlist: []
```

The full schema with documentation comments lives in `config.example.yaml`. 

**For dev/test against an unencrypted endpoint:**

Use `channel: {mode: "None", policy: "None"}` and `user: {type: anonymous}` — the wrapper rejects `username` over a `None` channel at config-load to prevent cleartext credentials.

A convenient upstream for local testing is the OPC Foundation [.NET Console Reference Server](https://github.com/OPCFoundation/UA-.NETStandard/tree/master/Applications/ConsoleReferenceServer) (a subdir of the [UA-.NETStandard](https://github.com/OPCFoundation/UA-.NETStandard) repo) — it exposes the standard `opc.tcp://<host>:62541/Quickstarts/ReferenceServer` endpoint that the `live` test marker in `pyproject.toml` targets.

## 2. Run via included DockerFile or CLI

CLI: 

```bash
./run.sh config.yaml
# or:
uv run i3xua --config config.yaml
```

Default API port is 8080. Bearer token comes from `server.auth.tokens`
in the config.

## Transport security

The wrapper supports two deployment modes:

- **Native TLS.** Set `server.tls.cert_path` and `server.tls.key_path` (and
  optionally `server.tls.key_password`) in your config.
  
mTLS, HSTS, HTTP→HTTPS redirect, and dual-port listening are not currently
supported.

## 3. Check status via UI

Open `http://localhost:8080/admin/ui` in a browser. Paste the bearer
token from your config when prompted.

The page shows live runtime status across the top. The bottom half has a 
**Run health check** button. Click it to generate (or refresh) the 
static-analysis report.

## License

i3xua is licensed under the MIT License. See [LICENSE](LICENSE)
and [NOTICE](NOTICE).

Third-party runtime dependencies retain their own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the list and attributions.
Of note, `asyncua` is LGPL-3.0-or-later and is used as an unmodified imported
library; redistributors bundling it with i3xua must comply with LGPLv3.
