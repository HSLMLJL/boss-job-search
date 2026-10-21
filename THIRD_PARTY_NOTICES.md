# Third-Party Dependency Notices

This document retains the dependency license inventory audited for the
upstream `boss-agent-cli` on 2026-09-04. Job Radar is distributed as
`job-radar-boss`; its current dependency declarations are in `pyproject.toml`.
It is an inventory for release review, not legal advice.

The `job-radar-boss` wheel declares its dependencies through
`Requires-Dist`. It does not copy their source code or binary artifacts into
the `job-radar-boss` wheel. Each dependency is installed as a separate,
replaceable Python distribution and retains its own copyright and license
files. Exact resolved versions can change within the declared constraints, so
maintainers must regenerate the dependency and license audit for every release.

| Direct dependency | Declared requirement | License metadata at audit | Upstream |
| --- | --- | --- | --- |
| aiohttp | `aiohttp>=3.9` | Apache-2.0 AND MIT | <https://github.com/aio-libs/aiohttp> |
| browser-cookie3 | `browser-cookie3>=0.16.2` | LGPL-3.0 | <https://github.com/borisbabic/browser_cookie3> |
| click | `click>=8.0` | BSD-3-Clause | <https://github.com/pallets/click> |
| cryptography | `cryptography>=42.0` | Apache-2.0 OR BSD-3-Clause | <https://github.com/pyca/cryptography> |
| httpx | `httpx[socks]>=0.27` | BSD-3-Clause | <https://github.com/encode/httpx> |
| openpyxl | `openpyxl>=3.1` | MIT | <https://foss.heptapod.net/openpyxl/openpyxl> |
| patchright | `patchright>=1.58.2` | Apache-2.0 | <https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python> |
| prompt-toolkit | `prompt-toolkit>=3.0.43` | BSD-3-Clause | <https://github.com/prompt-toolkit/python-prompt-toolkit> |
| PyYAML | `pyyaml>=6.0` | MIT | <https://github.com/yaml/pyyaml> |
| rich | `rich>=13.0` | MIT | <https://github.com/Textualize/rich> |
| websockets | `websockets>=12.0` | BSD-3-Clause | <https://github.com/python-websockets/websockets> |

The `socks` extra on the direct `httpx` requirement installs `socksio` as a
separate transitive distribution. The 1.0.0 release was audited as MIT and is
maintained at <https://github.com/sethmlarson/socksio>.

## LGPL dependency boundary

`browser-cookie3` 0.20.1 identified its license as the GNU Lesser General
Public License version 3 during this audit. Its package information is
available on [PyPI](https://pypi.org/project/browser-cookie3/), and the license
text is available from the [GNU project](https://www.gnu.org/licenses/lgpl-3.0.html).

The current distribution keeps `browser-cookie3` outside the
`job-radar-boss` wheel as a separately installed and replaceable dependency.
Before shipping a single-file executable, vendoring any dependency, copying
dependency code or binaries into this project, or distributing a modified
`browser-cookie3`, maintainers must perform a new license review and satisfy
all applicable LGPL notice, license-text, corresponding-source, and
relinking/replacement obligations.
