# Third-Party Notices

i3xua is licensed under the Apache License, Version 2.0 (see `LICENSE` and
`NOTICE`). It depends on the following third-party packages at runtime. Each
remains under its own license; this file is informational and does not
re-license any component.

## Runtime dependencies

| Package      | License                                | Project URL |
|--------------|----------------------------------------|-------------|
| asyncua      | **LGPL-3.0-or-later**                  | https://github.com/FreeOpcUa/opcua-asyncio |
| fastapi      | MIT                                    | https://github.com/fastapi/fastapi |
| starlette    | BSD-3-Clause (transitive via fastapi)  | https://github.com/encode/starlette |
| anyio        | MIT                                    | https://github.com/agronholm/anyio |
| httpx        | BSD-3-Clause                           | https://github.com/encode/httpx |
| httpx-sse    | MIT                                    | https://github.com/florimondmanca/httpx-sse |
| janus        | Apache-2.0                             | https://github.com/aio-libs/janus |
| pydantic     | MIT                                    | https://github.com/pydantic/pydantic |
| pyyaml       | MIT                                    | https://github.com/yaml/pyyaml |
| structlog    | MIT OR Apache-2.0                      | https://github.com/hynek/structlog |
| uvicorn      | BSD-3-Clause                           | https://github.com/encode/uvicorn |
| httptools    | MIT (uvicorn[standard] extra)          | https://github.com/MagicStack/httptools |
| websockets   | BSD-3-Clause (uvicorn[standard] extra) | https://github.com/python-websockets/websockets |
| watchfiles   | MIT (uvicorn[standard] extra)          | https://github.com/samuelcolvin/watchfiles |
| uvloop       | MIT / Apache-2.0 (uvicorn[standard] extra) | https://github.com/MagicStack/uvloop |
| python-dotenv | BSD-3-Clause (uvicorn[standard] extra) | https://github.com/theskumar/python-dotenv |

Transitive dependencies are not enumerated here; they are pinned in `uv.lock`
and may be inspected with `uv pip list` or `pip-licenses` against an installed
environment. To the best of our knowledge, all transitive runtime dependencies
are distributed under permissive licenses (MIT, BSD, Apache-2.0, ISC, PSF) or
the LGPL exception noted above.

## Note on `asyncua` (LGPL-3.0-or-later)

`asyncua` is the only non-permissive runtime dependency. i3xua imports it as
an unmodified library at runtime; it is not statically linked, vendored, or
modified. This is permitted under §5 of the LGPLv3 ("Combined Works"), and
Apache-2.0 is one of the licenses LGPLv3 explicitly permits combined works to
carry, provided the resulting distribution:

1. Carries prominent notice that `asyncua` is used and is licensed under the
   LGPL-3.0-or-later (this file and `NOTICE` satisfy that requirement).
2. Includes or points to a copy of the GNU LGPL v3 (see
   https://www.gnu.org/licenses/lgpl-3.0.txt) and the GNU GPL v3
   (https://www.gnu.org/licenses/gpl-3.0.txt).
3. Allows the end user to replace `asyncua` with a modified version. Because
   i3xua loads `asyncua` as a normal Python package via the standard import
   system, an end user can satisfy this by installing a different
   `asyncua` build into the same Python environment — no special tooling is
   required from i3xua.

If you redistribute i3xua **bundled together with `asyncua`** (e.g. inside a
container image, a frozen executable, or a wheel that vendors `asyncua`), you
must also make the corresponding `asyncua` source available to recipients per
LGPLv3 §4(d). The upstream source is at
https://github.com/FreeOpcUa/opcua-asyncio.

## Development-only dependencies

Tools listed under `[dependency-groups].dev` in `pyproject.toml` (pytest,
ruff, mypy, pyright, schemathesis, etc.) are not distributed with i3xua and
are not covered by this notice. Their licenses apply only to the development
environment.
