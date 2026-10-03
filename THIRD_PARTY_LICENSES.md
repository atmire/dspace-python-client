# Third-party licenses

This document lists the licenses of third-party software used by
**dspace-python-client**. The project itself is licensed under
**GPL-3.0-or-later**; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

License information was generated from installed packages
(`pip-licenses`, development environment with runtime, optional, and dev
dependencies). Versions may change as dependencies are updated; regenerate
this list before a release if needed.

## Direct runtime dependencies

| Package | License | URL |
|---------|---------|-----|
| defusedxml | Python Software Foundation License | https://github.com/tiran/defusedxml |
| GitPython | BSD-3-Clause | https://github.com/gitpython-developers/GitPython |
| httpx | BSD-3-Clause | https://github.com/encode/httpx |
| orjson | Apache-2.0 OR MIT | https://github.com/ijl/orjson |
| python-dateutil | Apache-2.0; BSD-3-Clause | https://github.com/dateutil/dateutil |
| rich | MIT | https://github.com/Textualize/rich |
| tenacity | Apache-2.0 | https://github.com/jd/tenacity |
| typer | MIT | https://github.com/fastapi/typer |

## Optional dependencies (examples)

| Package | License | URL |
|---------|---------|-----|
| PyYAML | MIT | https://pyyaml.org/ |

### `a11y` extra (examples/pdf_accessibility)

| Package | License | URL |
|---------|---------|-----|
| outloud | Apache-2.0 | https://github.com/visionably/outloud |
| PyMuPDF | AGPL-3.0 (or Artifex commercial licence) | https://github.com/pymupdf/PyMuPDF |
| pikepdf | MPL-2.0 | https://github.com/pikepdf/pikepdf |
| fonttools | MIT | https://github.com/fonttools/fonttools |
| click | BSD-3-Clause | https://github.com/pallets/click |

PyMuPDF is AGPL-3.0, which is compatible with this project's GPL-3.0-or-later
(GPLv3 section 13). It is only installed with the optional `a11y` extra and is
used by a script an administrator runs locally; anyone who offers that code to
others over a network should check the AGPL's terms.

### `a11y-ocr` extra (OCR remediation track)

| Package | License | URL |
|---------|---------|-----|
| rapidocr | Apache-2.0 | https://github.com/RapidAI/RapidOCR |
| onnxruntime | MIT | https://github.com/microsoft/onnxruntime |
| opencv-python | Apache-2.0 | https://github.com/opencv/opencv-python |
| numpy | BSD-3-Clause | https://numpy.org |
| shapely | BSD-3-Clause | https://github.com/shapely/shapely |
| pyclipper | MIT | https://github.com/fonttools/pyclipper |

The OCR models (PaddleOCR PP-OCR, Apache-2.0) ship inside the rapidocr wheel.

## Development and test dependencies

| Package | License | URL |
|---------|---------|-----|
| mypy | MIT | https://www.mypy-lang.org/ |
| pytest | MIT | https://docs.pytest.org/ |
| pytest-asyncio | Apache-2.0 | https://github.com/pytest-dev/pytest-asyncio |
| respx | BSD-3-Clause | https://lundberg.github.io/respx/ |
| ruff | MIT | https://docs.astral.sh/ruff |

## Transitive dependencies

The following packages are pulled in indirectly (HTTP stack, GitPython,
Rich, Typer, pytest, mypy, and related tooling). All are compatible with
GPL-3.0-or-later.

| Package | License | URL |
|---------|---------|-----|
| anyio | MIT | https://anyio.readthedocs.io/ |
| ast_serialize | MIT | https://github.com/mypyc/ast_serialize |
| certifi | MPL-2.0 | https://github.com/certifi/python-certifi |
| click | BSD-3-Clause | https://github.com/pallets/click/ |
| gitdb | BSD-3-Clause | https://github.com/gitpython-developers/gitdb |
| h11 | MIT | https://github.com/python-hyper/h11 |
| h2 | MIT | https://github.com/python-hyper/h2/ |
| hpack | MIT | https://github.com/python-hyper/hpack/ |
| httpcore | BSD-3-Clause | https://www.encode.io/httpcore/ |
| hyperframe | MIT | https://github.com/python-hyper/hyperframe/ |
| idna | BSD-3-Clause | https://github.com/kjd/idna |
| iniconfig | MIT | https://github.com/pytest-dev/iniconfig |
| librt | MIT | https://github.com/mypyc/librt |
| markdown-it-py | MIT | https://github.com/executablebooks/markdown-it-py |
| mdurl | MIT | https://github.com/executablebooks/mdurl |
| mypy_extensions | MIT | https://github.com/python/mypy_extensions |
| packaging | Apache-2.0; BSD-3-Clause | https://github.com/pypa/packaging |
| pathspec | MPL-2.0 | https://python-path-specification.readthedocs.io/ |
| pluggy | MIT | https://github.com/pytest-dev/pluggy |
| Pygments | BSD-3-Clause | https://pygments.org |
| shellingham | ISC | https://github.com/sarugaku/shellingham |
| six | MIT | https://github.com/benjaminp/six |
| smmap | BSD-3-Clause | https://github.com/gitpython-developers/smmap |
| sniffio | Apache-2.0; MIT | https://github.com/python-trio/sniffio |
| typing_extensions | PSF-2.0 | https://github.com/python/typing_extensions |

## Build dependencies

Build tooling (for example **hatchling**) is MIT-licensed and is not
distributed as part of the installed Python package.

## External services (examples only)

Some example scripts call public HTTP APIs (for example Unpaywall, OpenAlex,
Crossref). Those services have their own terms of use; they are not Python
package dependencies and are not covered by the licenses above.
