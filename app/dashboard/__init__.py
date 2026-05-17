import atexit
import json
import os
import subprocess
from pathlib import Path

from app import app
from config import DEBUG, VITE_BASE_API, DASHBOARD_PATH
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

base_dir = Path(__file__).parent
build_dir = base_dir / 'build'
statics_dir = build_dir / 'statics'
env_script_path = os.path.join(DASHBOARD_PATH, 'env.js')


@app.get(env_script_path, include_in_schema=False)
def dashboard_env():
    return Response(
        "window.__MARZBAN_CONFIG__ = "
        + json.dumps({"baseApi": VITE_BASE_API})
        + ";",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


def ensure_runtime_env_script():
    script = f'<script src="{env_script_path}"></script>'
    module_script = '<script type="module"'

    for filename in ("index.html", "404.html"):
        path = build_dir / filename
        if not path.is_file():
            continue

        html = path.read_text()
        if env_script_path in html:
            continue

        if module_script in html:
            html = html.replace(module_script, f'{script}\n    {module_script}', 1)
        else:
            html = html.replace("</body>", f"    {script}\n  </body>", 1)
        path.write_text(html)


def build():
    proc = subprocess.Popen(
        ['npm', 'run', 'build', '--',  '--outDir', build_dir, '--assetsDir', 'statics'],
        env={**os.environ, 'VITE_BASE_API': VITE_BASE_API},
        cwd=base_dir
    )
    proc.wait()
    with open(build_dir / 'index.html', 'r') as file:
        html = file.read()
    with open(build_dir / '404.html', 'w') as file:
        file.write(html)
    ensure_runtime_env_script()


def run_dev():
    proc = subprocess.Popen(
        ['npm', 'run', 'dev', '--', '--host', '0.0.0.0', '--clearScreen', 'false', '--base', os.path.join(DASHBOARD_PATH, '')],
        env={**os.environ, 'VITE_BASE_API': VITE_BASE_API},
        cwd=base_dir
    )

    atexit.register(proc.terminate)


def run_build():
    if not build_dir.is_dir():
        build()
    ensure_runtime_env_script()

    app.mount(
        DASHBOARD_PATH,
        StaticFiles(directory=build_dir, html=True),
        name="dashboard"
    )
    app.mount(
        '/statics/',
        StaticFiles(directory=statics_dir, html=True),
        name="statics"
    )


@app.on_event("startup")
def startup():
    if DEBUG:
        run_dev()
    else:
        run_build()
