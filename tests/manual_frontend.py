"""Opt-in network smoke test: real npm installs, builds and browser interactions.

Run with the aa2 environment and an installed AA2_BROWSER_CACHE. This script creates
its own temporary project, prints its location, and retains source and evidence.
"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from auto_agents_2.config import ConfigStore
from auto_agents_2.design import inspect_application
from auto_agents_2.environment import EnvironmentManager
from auto_agents_2.models import BrowserAction, Command, PreviewSpec
from auto_agents_2.resources import ResourceManager
from auto_agents_2.sandbox import SandboxRunner
from auto_agents_2.store import Store
from auto_agents_2.util import write_json
from auto_agents_2.verification import VerificationService, passed

COUNTER = """'use client';
import React, {useState} from 'react';
export default function Counter() {
  const [n, setN] = useState(0);
  return <main><h1>AA2 counter</h1>
    <button id="increment" onClick={() => setN(n + 1)}>Add</button>
    <output id="count">{n}</output></main>;
}
"""


def fixture(work, framework):
    work.mkdir()
    package = {"name": f"aa2-{framework}-fixture", "version": "0.0.0", "private": True, "type": "module",
               "dependencies": {"react": "19.3.0", "react-dom": "19.3.0"},
               "devDependencies": {"typescript": "5.9.3", "@types/react": "^19", "@types/react-dom": "^19", "@types/node": "^22"}}
    if framework == "react":
        package["devDependencies"]["vite"] = "8.3.4"
        package["scripts"] = {"build": "tsc --noEmit && vite build", "preview": "vite preview --host 127.0.0.1"}
        (work / "index.html").write_text('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module" src="/main.tsx"></script></body></html>')
        (work / "Counter.tsx").write_text(COUNTER.replace("'use client';\n", ""))
        (work / "main.tsx").write_text("import React from 'react'; import {createRoot} from 'react-dom/client'; import Counter from './Counter'; createRoot(document.getElementById('root')!).render(<Counter/>);\n")
        write_json(work / "tsconfig.json", {"compilerOptions": {"target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler", "jsx": "react-jsx", "noEmit": True, "strict": True, "skipLibCheck": True}})
        preview = ["npm", "run", "preview", "--", "--port", "{port}"]
    else:
        package["dependencies"]["next"] = "16.4.0"
        package["scripts"] = {"build": "next build --webpack", "start": "next start --hostname 127.0.0.1"}
        (work / "app").mkdir()
        (work / "app/page.tsx").write_text(COUNTER)
        (work / "app/layout.tsx").write_text("import React from 'react'; export default function Layout({children}:{children:React.ReactNode}) { return <html lang='en'><body>{children}</body></html>; }\n")
        (work / "next.config.mjs").write_text("export default {experimental:{cpus:2}};\n")
        write_json(work / "tsconfig.json", {"compilerOptions": {
            "target": "ES2017", "lib": ["dom", "dom.iterable", "esnext"], "allowJs": True,
            "skipLibCheck": True, "strict": True, "noEmit": True, "esModuleInterop": True,
            "module": "esnext", "moduleResolution": "bundler", "resolveJsonModule": True,
            "isolatedModules": True, "jsx": "react-jsx", "incremental": True,
            "tsBuildInfoFile": "./node_modules/.tmp/next.tsbuildinfo", "plugins": [{"name": "next"}]},
            "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts", ".next/dev/types/**/*.ts"],
            "exclude": ["node_modules"]})
        preview = ["npm", "run", "start", "--", "--port", "{port}"]
    write_json(work / "package.json", package)
    return PreviewSpec(command=Command(argv=preview), actions=[
        BrowserAction(action="click", selector="#increment"),
        BrowserAction(action="expect_text", selector="#count", value="1")])


async def main():
    project = Path(tempfile.mkdtemp(prefix="aa2-frontend-smoke-"))
    print(f"Fixture and evidence: {project}", flush=True)
    config = ConfigStore(project)
    config.initialize()
    settings = config.load().model_dump(mode="json")
    settings["cleanup"].update(cache_max_bytes=0, cache_ttl_seconds=0, min_free_bytes=0)
    write_json(config.path, settings)
    store = Store(project)
    run = store.admit("fixture://frontend", {})
    root = store.root
    runner = SandboxRunner(root)
    manager = EnvironmentManager(config, runner)
    cache = os.environ.get("AA2_BROWSER_CACHE", str(Path(__file__).parents[1] / ".cache/browsers"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = cache
    results = {}
    try:
        for framework in ("react", "next"):
            work = project / framework
            preview = fixture(work, framework)
            print(f"Preparing {framework}", flush=True)
            environment = await manager.ensure(work, "python-react")
            await manager.install_node(work, environment)
            checks = await VerificationService(root, runner).run([Command(argv=["npm", "run", "build"], timeout=300)], work, environment)
            results[framework] = {"checks": [r.model_dump() for r in checks]}
            assert passed(checks), checks[0].output
            visuals = await inspect_application(preview, work, root / "visuals" / framework, environment, runner, root)
            results[framework]["browser"] = visuals
            print(f"Passed {framework}: production build, desktop and mobile click assertions", flush=True)
        store.update(run["id"], status="SUCCEEDED", outcome="DIAGNOSTIC")
    except BaseException:
        store.update(run["id"], status="CANCELLED", outcome="DIAGNOSTIC_FAILED")
        raise
    finally:
        write_json(project / "results.json", results)
        ResourceManager(store, config.load().cleanup).collect()
        for framework in ("react", "next"):
            for generated in ("node_modules", ".next", "dist"):
                shutil.rmtree(project / framework / generated, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
